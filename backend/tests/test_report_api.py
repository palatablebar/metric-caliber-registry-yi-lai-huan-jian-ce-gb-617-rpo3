"""HTTP 层测试：报表登记 / as_of 时点 / 导入幂等 / RBAC（SQLite + TestClient）。"""
from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def client(monkeypatch):
    def _sqlite_engine(url, *args, **kwargs):
        # 单连接 + StaticPool，让 TestClient 工作线程与主线程共享同一个内存库。
        kwargs.pop("pool_size", None)
        kwargs.pop("max_overflow", None)
        kwargs.pop("pool_recycle", None)
        kwargs.pop("future", None)
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
        return _sa_create_engine("sqlite+pysqlite:///:memory:", future=True, **kwargs)

    monkeypatch.setattr(sqlalchemy, "create_engine", _sqlite_engine)
    from src.config import settings as settings_mod

    monkeypatch.setattr(
        settings_mod.Settings, "database_url", property(lambda self: "sqlite+pysqlite:///:memory:")
    )
    monkeypatch.setattr(settings_mod.settings, "seed_demo", False)

    from src.config import database as dbmod
    from src.models import Base

    # 模块级 engine 在测试间复用，每个用例前清空并重建全部表，保证隔离。
    Base.metadata.drop_all(dbmod.engine)
    Base.metadata.create_all(dbmod.engine)

    # 关 Redis 心跳依赖，避免 overview 卡住
    from src.services import overview_service

    monkeypatch.setattr(overview_service, "worker_alive", lambda: False)

    from src.config.app import create_app

    # 只建三个登录账号，不跑完整 seed（测试数据按需手动塞）。
    from scripts.seed_demo import ensure_users

    with dbmod.SessionLocal() as db:
        ensure_users(db)
        db.commit()

    app = create_app()
    with TestClient(app) as client:
        yield client, dbmod


def _token(client, email, password="Demo@2026"):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_minimal(dbmod):
    from src.constants.enums import ReportStatus
    from src.models.report import Report

    with dbmod.SessionLocal() as db:
        db.add_all(
            [
                Report(
                    code="r00", name="旧日报", owner="林可", is_core=True, weight=3,
                    metric_codes=["gmv"], valid_from=date(2024, 1, 1), valid_to=date(2025, 9, 1),
                    status=ReportStatus.RETIRED.value,
                ),
                Report(
                    code="r01", name="新日报", owner="林可", is_core=True, weight=3,
                    metric_codes=["gmv"], valid_from=date(2025, 9, 1), valid_to=None,
                    status=ReportStatus.ACTIVE.value,
                ),
            ]
        )
        db.commit()


def test_reports_default_hides_retired_and_as_of_restores(client):
    http, dbmod = client
    _seed_minimal(dbmod)
    token = _token(http, "viewer@metricreg.io")

    resp = http.get("/api/reports", headers=_auth(token))
    assert resp.status_code == 200
    codes = {item["code"] for item in resp.json()["items"]}
    assert codes == {"r01"}

    resp = http.get("/api/reports?as_of=2025-01-01", headers=_auth(token))
    assert resp.json()["items"][0]["code"] == "r00"

    resp = http.get("/api/reports?status=retired", headers=_auth(token))
    assert {item["code"] for item in resp.json()["items"]} == {"r00"}


def test_reports_as_of_rejects_bad_date(client):
    http, _ = client
    token = _token(http, "viewer@metricreg.io")
    resp = http.get("/api/reports?as_of=nope", headers=_auth(token))
    assert resp.status_code == 422


def test_import_requires_admin_and_is_idempotent(client):
    http, dbmod = client
    viewer = _token(http, "viewer@metricreg.io")
    analyst = _token(http, "analyst@metricreg.io")
    admin = _token(http, "admin@metricreg.io")

    payload = {
        "batch_no": "api-batch-001",
        "reports": [{"code": "r50", "name": "接口报表", "metric_codes": ["gmv"]}],
    }
    # viewer 403、analyst 403（仅管理员可登记）
    assert http.post("/api/reports/imports", json=payload, headers=_auth(viewer)).status_code == 403
    assert http.post("/api/reports/imports", json=payload, headers=_auth(analyst)).status_code == 403

    first = http.post("/api/reports/imports", json=payload, headers=_auth(admin))
    assert first.status_code == 201
    assert first.json()["inserted"] == 1 and first.json()["replayed"] is False

    second = http.post("/api/reports/imports", json=payload, headers=_auth(admin))
    assert second.status_code == 201
    assert second.json()["replayed"] is True

    from src.models.report import Report, ReportImportBatch
    from sqlalchemy import func, select

    with dbmod.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Report)) == 1
        assert db.scalar(select(func.count()).select_from(ReportImportBatch)) == 1


def test_late_active_row_does_not_revive_retired(client):
    http, _ = client
    admin = _token(http, "admin@metricreg.io")
    http.post(
        "/api/reports/imports",
        json={
            "batch_no": "api-retire",
            "reports": [{
                "code": "r60", "name": "将停用", "status": "retired",
                "valid_from": "2025-01-01", "valid_to": "2026-01-01",
            }],
        },
        headers=_auth(admin),
    )
    revive = http.post(
        "/api/reports/imports",
        json={"batch_no": "api-revive", "reports": [{"code": "r60", "name": "复活", "status": "active"}]},
        headers=_auth(admin),
    )
    assert revive.json()["skipped"] == 1 and revive.json()["inserted"] == 0
    listing = http.get("/api/reports", headers=_auth(admin)).json()
    assert all(item["code"] != "r60" for item in listing["items"])


def test_reports_csv_export_rbac_and_as_of(client):
    http, _ = client
    _seed_minimal(client[1])
    viewer = _token(http, "viewer@metricreg.io")
    admin = _token(http, "admin@metricreg.io")

    assert http.get("/api/exports/reports.csv", headers=_auth(viewer)).status_code == 403
    resp = http.get("/api/exports/reports.csv?as_of=2025-01-01", headers=_auth(admin))
    assert resp.status_code == 200
    assert "r00".encode() in resp.content and "2025-01-01".encode() in resp.content


def test_lineage_endpoint(client):
    http, _ = client
    admin = _token(http, "admin@metricreg.io")
    http.post(
        "/api/reports/imports",
        json={
            "batch_no": "api-lineage",
            "reports": [{"code": "r70", "name": "源", "status": "retired",
                         "valid_from": "2025-01-01", "valid_to": "2026-01-01"}],
            "lineage": [{
                "relation_kind": "rename", "source_code": "r70", "target_code": "r71",
                "effective_on": "2026-01-01", "note": "改名",
            }],
        },
        headers=_auth(admin),
    )
    resp = http.get("/api/reports/lineages?code=r70", headers=_auth(admin))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["target_code"] == "r71"


def test_unauthenticated_401(client):
    http, _ = client
    assert http.get("/api/reports").status_code == 401
