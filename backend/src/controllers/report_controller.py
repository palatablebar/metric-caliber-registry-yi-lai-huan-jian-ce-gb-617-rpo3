"""报表控制器：登记列表（支持时点还原）/ 血缘 / 清单批次导入。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, report_service


def list_reports(db: Session, **filters) -> dict:
    return report_service.list_reports(db, **filters)


def detail(db: Session, report_id: int) -> dict:
    report = report_service.get_report(db, report_id)
    lineage = report_service.list_lineage(db, code=report.code, limit=50)["items"]
    return report_service.serialize(report, lineage=lineage)


def list_lineage(db: Session, *, code: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    return report_service.list_lineage(db, code=code, limit=limit, offset=offset)


def list_batches(db: Session, *, limit: int = 50, offset: int = 0) -> dict:
    return report_service.list_batches(db, limit=limit, offset=offset)


def import_batch(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    result = report_service.import_batch(db, payload, operator_email=principal["email"])
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="report.import",
        entity="report",
        entity_id=result["batch_no"],
        detail={
            "batch_no": result["batch_no"],
            "replayed": result["replayed"],
            "inserted": result["inserted"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "lineage_added": result["lineage_added"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result
