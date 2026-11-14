"""导出控制器：五张 CSV + 审计；支持 as_of 时点还原。"""
from __future__ import annotations

from fastapi import Request, Response
from sqlalchemy.orm import Session

from src.services import audit_service, export_service
from src.utils.csv_export import csv_response_headers

CSV_MEDIA = "text/csv; charset=utf-8"


def _respond(db: Session, content: bytes, filename: str, action: str, entity: str, principal: dict, request: Request,
             as_of: str | None = None):
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action=action,
        entity=entity,
        entity_id=None,
        detail={"bytes": len(content), "file": filename, "as_of": as_of},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return Response(content=content, media_type=CSV_MEDIA, headers=csv_response_headers(filename))


def metrics_csv(db: Session, principal: dict, request: Request, *, as_of: str | None = None) -> Response:
    return _respond(
        db, export_service.metrics_csv(db, as_of=as_of), "metrics.csv",
        "export.metrics", "metric", principal, request, as_of,
    )


def reports_csv(db: Session, principal: dict, request: Request, *, as_of: str | None = None) -> Response:
    return _respond(
        db, export_service.reports_csv(db, as_of=as_of), "reports.csv",
        "export.reports", "report", principal, request, as_of,
    )


def conflicts_csv(db: Session, principal: dict, request: Request, *, as_of: str | None = None) -> Response:
    return _respond(
        db, export_service.conflicts_csv(db, as_of=as_of), "conflicts.csv",
        "export.conflicts", "conflict", principal, request, as_of,
    )


def changes_csv(db: Session, principal: dict, request: Request) -> Response:
    return _respond(
        db,
        export_service.change_requests_csv(db),
        "change-requests.csv",
        "export.changes",
        "change_request",
        principal,
        request,
    )


def runs_csv(db: Session, principal: dict, request: Request) -> Response:
    return _respond(db, export_service.runs_csv(db), "runs.csv", "export.runs", "run", principal, request)
