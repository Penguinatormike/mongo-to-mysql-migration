from dataclasses import replace
from datetime import datetime

import pytest

from conftest import make_account
from migrator.models import truncate_ms


def test_fingerprint_ignores_settings_key_order():
    a = make_account(settings={"a": 1, "b": 2})
    b = replace(a, settings={"b": 2, "a": 1})
    assert a.fingerprint() == b.fingerprint()


@pytest.mark.parametrize("field,value", [("email", "x@example.com"), ("version", 2), ("settings", {"theme": "light"})])
def test_fingerprint_changes_with_content(field, value):
    a = make_account()
    assert a.fingerprint() != replace(a, **{field: value}).fingerprint()


def test_fingerprint_ignores_sub_millisecond_precision():
    a = make_account(updated_at=datetime(2026, 1, 1, 12, 0, 0, 123000))
    b = replace(a, updated_at=datetime(2026, 1, 1, 12, 0, 0, 123999))
    assert a.fingerprint() == b.fingerprint()


def test_truncate_ms():
    assert truncate_ms(datetime(2026, 1, 1, 0, 0, 0, 123456)).microsecond == 123000
