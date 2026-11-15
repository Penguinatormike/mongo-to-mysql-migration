"""Compare the two stores record by record, and optionally repair the target from the source of truth."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ReconcileReport:
    source: str
    target: str
    checked: int = 0
    missing: list[str] = field(default_factory=list)  # in source, not in target
    mismatched: list[str] = field(default_factory=list)  # in both, contents differ
    extra: list[str] = field(default_factory=list)  # in target, not in source (e.g. a resurrected delete)
    repaired: int = 0
    transient: int = 0  # differences that disappeared on re-check: in-flight writes, not drift

    @property
    def drift(self) -> int:
        return len(self.missing) + len(self.mismatched) + len(self.extra)

    @property
    def clean(self) -> bool:
        return self.drift == 0

    def summary(self) -> str:
        return (
            f"checked={self.checked:,} missing={len(self.missing)} mismatched={len(self.mismatched)} "
            f"extra={len(self.extra)} transient={self.transient} repaired={self.repaired}"
        )


def _classify(src_doc, dst_doc) -> str | None:
    if src_doc is None and dst_doc is None:
        return None
    if dst_doc is None:
        return "missing"
    if src_doc is None:
        return "extra"
    if src_doc.fingerprint() != dst_doc.fingerprint():
        return "mismatched"
    return None


def reconcile(
    mongo,
    mysql,
    source: str = "mongo",
    repair: bool = False,
    batch_size: int = 1000,
    settle: float = 0.25,
) -> ReconcileReport:
    stores = {"mongo": mongo, "mysql": mysql}
    target = "mysql" if source == "mongo" else "mongo"
    src, dst = stores[source], stores[target]
    report = ReconcileReport(source=source, target=target)
    suspects: set[str] = set()

    # Pass 1: every source record exists in the target with identical contents.
    for batch in src.iter_batches(batch_size):
        found = dst.get_many([a.id for a in batch])
        for a in batch:
            report.checked += 1
            if _classify(a, found.get(a.id)):
                suspects.add(a.id)

    # Pass 2: the target has nothing the source doesn't.
    for batch in dst.iter_batches(batch_size):
        found = src.get_many([a.id for a in batch])
        suspects.update(a.id for a in batch if a.id not in found)

    # With live traffic, a write can land between reading the source and the target.
    # Re-read the suspects after a short pause, and only count differences that persist.
    if suspects:
        if settle:
            time.sleep(settle)
        ids = sorted(suspects)
        src_now, dst_now = src.get_many(ids), dst.get_many(ids)
        for id in ids:
            kind = _classify(src_now.get(id), dst_now.get(id))
            if kind is None:
                report.transient += 1
            else:
                getattr(report, kind).append(id)

    if repair:
        for id in report.missing + report.mismatched + report.extra:
            current = src.get(id)  # re-read: the source of truth may have moved on
            if current is None:
                dst.delete(id)
            else:
                dst.force_put(current)
            report.repaired += 1

    return report


def reconcile_until_clean(
    mongo,
    mysql,
    source: str = "mongo",
    max_passes: int = 5,
    log: Callable[[str], None] | None = None,
) -> ReconcileReport:
    """Repair, then re-verify, until a pass finds no drift."""
    report = None
    for n in range(1, max_passes + 1):
        report = reconcile(mongo, mysql, source=source, repair=True)
        if log:
            log(f"  pass {n}: {report.summary()}")
        if report.clean:
            return report
    return report
