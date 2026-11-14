from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import METRIC_WRITE_ROLES, READ_ROLES
from src.controllers import report_controller
from src.middlewares.rbac import require_roles
from src.schemas.report import ReportImportIn

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("")
def list_reports(
    core: bool | None = None,
    q: str | None = None,
    status: str | None = None,
    as_of: str | None = None,
    include_retired: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return report_controller.list_reports(
        db,
        core=core,
        q=q,
        status=status,
        as_of=as_of,
        include_retired=include_retired,
        limit=limit,
        offset=offset,
    )


@router.get("/lineages")
def list_lineages(
    code: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return report_controller.list_lineage(db, code=code, limit=limit, offset=offset)


@router.get("/import-batches")
def list_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return report_controller.list_batches(db, limit=limit, offset=offset)


@router.get("/{report_id}")
def report_detail(
    report_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return report_controller.detail(db, report_id)


@router.post("/imports", status_code=201)
def import_reports(
    payload: ReportImportIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*METRIC_WRITE_ROLES)),
) -> dict:
    return report_controller.import_batch(db, payload.model_dump(), principal, request)
