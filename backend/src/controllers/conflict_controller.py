"""冲突控制器：同名口径冲突（算法 B 实时计算，不落冗余表），支持时点还原。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.services import graph_service
from src.utils.errors import AppError


def list_conflicts(
    db: Session,
    *,
    severity: str | None = None,
    kind: str | None = None,
    as_of: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    items = graph_service.find_conflicts(db, as_of=as_of)
    if severity:
        items = [item for item in items if item["severity"] == severity]
    if kind:
        items = [item for item in items if item["kind"] == kind]
    total = len(items)
    return {
        "items": items[offset : offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "as_of": as_of,
    }


def detail(db: Session, conflict_id: str, *, as_of: str | None = None) -> dict:
    for item in graph_service.find_conflicts(db, as_of=as_of):
        if item["id"] == conflict_id:
            return item
    raise AppError("NOT_FOUND", 404, {"entity": "conflict", "id": conflict_id})
