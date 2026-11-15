"""Run the test suite under coverage and fail if any core file is below the threshold.

Usage: python scripts/check_coverage.py [--min 80]
Needs MongoDB and MySQL reachable (see migrator/config.py), same as the tests.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

import coverage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min", type=float, default=80.0, help="minimum line coverage per core file (%%)")
    args = parser.parse_args()

    tests = subprocess.run([sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q", "-p", "no:warnings"])
    if tests.returncode != 0:
        print("\ncoverage check: tests failed", file=sys.stderr)
        return tests.returncode

    cov = coverage.Coverage()
    cov.load()
    cov.report()

    data = cov.get_data()
    failures = []
    for path in sorted(data.measured_files()):
        _, statements, _, missing, _ = cov.analysis2(path)
        if not statements:
            continue
        pct = 100.0 * (len(statements) - len(missing)) / len(statements)
        if pct < args.min:
            failures.append(f"  {path}: {pct:.1f}%")

    if failures:
        print(f"\ncoverage check: these core files are below {args.min:.0f}%:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"\ncoverage check: every core file is at or above {args.min:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
