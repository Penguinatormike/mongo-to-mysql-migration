import pytest
from fastapi.testclient import TestClient

from conftest import DUAL_WRITE, RUNBOOK
from migrator.app import create_app
from migrator.migration.flags import FixedSettings
from migrator.migration.repository import AccountRepository


@pytest.mark.parametrize("flags", RUNBOOK, ids=lambda f: f.describe())
def test_crud_works_under_every_runbook_step(stores, flags):
    mongo, mysql, _ = stores
    client = TestClient(create_app(AccountRepository(mongo, mysql, FixedSettings(flags))))

    r = client.post("/accounts", json={"email": "kim@example.com", "display_name": "Kim", "settings": {"x": 1}})
    assert r.status_code == 201
    account = r.json()
    assert account["plan"] == "free"

    assert client.get(f"/accounts/{account['id']}").json() == account

    r = client.patch(f"/accounts/{account['id']}", json={"plan": "pro"})
    assert r.status_code == 200 and r.json()["plan"] == "pro"

    assert client.delete(f"/accounts/{account['id']}").status_code == 204
    assert client.get(f"/accounts/{account['id']}").status_code == 404


def test_validation_and_not_found(stores):
    mongo, mysql, _ = stores
    client = TestClient(create_app(AccountRepository(mongo, mysql, FixedSettings())))
    assert client.post("/accounts", json={"email": "a@b.c", "display_name": "x", "plan": "gold"}).status_code == 422
    assert client.get("/accounts/not-an-id").status_code == 404
    assert client.patch("/accounts/0123456789abcdef01234567", json={"plan": "pro"}).status_code == 404
    assert client.patch("/accounts/0123456789abcdef01234567", json={}).status_code == 400
    assert client.delete("/accounts/0123456789abcdef01234567").status_code == 404


def test_health_and_metrics_report_the_flags(stores):
    mongo, mysql, _ = stores
    client = TestClient(create_app(AccountRepository(mongo, mysql, FixedSettings(DUAL_WRITE))))
    expected = {"write_mysql": True, "read_mysql": False, "mysql_primary": False, "write_mongo": True}
    assert client.get("/health").json() == {"status": "ok", "flags": expected}
    assert client.get("/metrics").json()["flags"] == expected
