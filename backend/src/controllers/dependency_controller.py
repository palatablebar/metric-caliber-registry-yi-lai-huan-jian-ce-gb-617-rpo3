"""依赖图控制器：边列表 + 图结构 + 环路径，支持 as_of 时点还原。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.services import dependency_service, graph_service


def list_edges(db: Session, **filters) -> dict:
    return dependency_service.list_edges(db, **filters)


def graph(db: Session, *, as_of: str | None = None) -> dict:
    return graph_service.graph(db, as_of=as_of)


def cycles(db: Session, *, as_of: str | None = None) -> dict:
    cycles = graph_service.find_cycles(db, as_of=as_of)
    return {"items": cycles, "total": len(cycles), "as_of": as_of}


def impact(db: Session, *, field: str | None = None, metric: str | None = None, as_of: str | None = None) -> dict:
    if field:
        return graph_service.impact_for_field(db, field, as_of=as_of)
    if metric:
        return graph_service.impact_for_metric(db, metric, as_of=as_of)
    return {"kind": "none", "score": 0, "severity": "low", "reports": [], "report_count": 0, "as_of": as_of}
