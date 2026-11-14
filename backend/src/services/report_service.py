"""报表服务：生效期间登记 + 指标 -> 报表反向索引（算法 B 的输入）+ 清单导入。

时点语义（所有读接口共享同一套规则）：
- 一条报表登记在时点 ``as_of`` 有效，当且仅当
  ``valid_from <= as_of`` 且（``valid_to`` 为空或 ``as_of < valid_to``）；
- ``as_of`` 为空时按「当前」处理，等价于只取 status=active 的登记；
- 改名 / 拆分 / 合并 / 停用不原地改写旧登记：旧登记置 retired 并写 valid_to，
  新登记另起一条 active，前后归属落在 ReportLineage（只追加）。

清单导入（import_batch）：
- batch_no 是幂等键：重跑同一批次直接返回首次结果，不新增任何记录；
- 已停用（retired）的 code 不会被迟到/乱序记录复活；
- 同一 code 的 active 登记只更新可变属性，生效日 valid_from 不可变。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    REPORT_LINEAGE_KIND_LABELS,
    REPORT_STATUS_LABELS,
    REPORT_WEIGHT_CORE,
    REPORT_WEIGHT_NORMAL,
    ReportLineageKind,
    ReportStatus,
)
from src.models.report import Report, ReportImportBatch, ReportLineage
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, local_now, parse_date

# 比任何业务日期都早的哨兵日，等价于「自登记体系建立起即有效」。
EPOCH_DATE = date(2000, 1, 1)


def _coerce_as_of(as_of: date | str | None) -> date | None:
    if as_of is None or isinstance(as_of, date):
        return as_of
    parsed = parse_date(str(as_of))
    if parsed is None:
        raise AppError("VALIDATION", 422, {"field": "as_of", "message": "as_of 需是 YYYY-MM-DD"})
    return parsed


def _coerce_valid_date(value: Any, *, field: str, default: date | None = None) -> date | None:
    if value in (None, ""):
        return default
    if isinstance(value, date):
        return value
    parsed = parse_date(str(value))
    if parsed is None:
        raise AppError("VALIDATION", 422, {"field": field, "message": f"{field} 需是 YYYY-MM-DD"})
    return parsed


def is_effective(report: Report, as_of: date | None) -> bool:
    """登记在 as_of 时点是否有效；as_of 为 None 表示「当前」（status=active）。"""
    if as_of is None:
        return report.status == ReportStatus.ACTIVE.value
    if report.valid_from and report.valid_from > as_of:
        return False
    if report.valid_to is not None and as_of >= report.valid_to:
        return False
    return True


def serialize(report: Report, *, lineage: list[dict] | None = None) -> dict:
    return {
        "id": report.id,
        "code": report.code,
        "name": report.name,
        "owner": report.owner,
        "channel": report.channel,
        "is_core": report.is_core,
        "weight": report.weight,
        "metric_codes": list(report.metric_codes or []),
        "metric_count": len(report.metric_codes or []),
        "refresh_cron": report.refresh_cron,
        "valid_from": report.valid_from.isoformat() if report.valid_from else None,
        "valid_to": report.valid_to.isoformat() if report.valid_to else None,
        "status": report.status,
        "status_label": REPORT_STATUS_LABELS.get(report.status, report.status),
        "lineage": lineage or [],
        "seed_key": report.seed_key,
        "created_at": iso_utc(report.created_at),
        "updated_at": iso_utc(report.updated_at),
    }


def _base_filters(stmt, *, core: bool | None, q: str | None):
    if core is not None:
        stmt = stmt.where(Report.is_core.is_(core))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Report.name.ilike(like) | Report.code.ilike(like))
    return stmt


def list_reports(
    db: Session,
    *,
    core: bool | None = None,
    q: str | None = None,
    status: str | None = None,
    as_of: date | str | None = None,
    include_retired: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    as_of_date = _coerce_as_of(as_of)
    stmt = select(Report)
    count_stmt = select(func.count()).select_from(Report)
    stmt = _base_filters(stmt, core=core, q=q)
    count_stmt = _base_filters(count_stmt, core=core, q=q)

    if as_of_date is not None:
        # 时点还原：落在生效期间内的登记（含当时 active、后来才停用的记录）。
        stmt = stmt.where(Report.valid_from <= as_of_date).where(
            (Report.valid_to.is_(None)) | (Report.valid_to > as_of_date)
        )
        count_stmt = count_stmt.where(Report.valid_from <= as_of_date).where(
            (Report.valid_to.is_(None)) | (Report.valid_to > as_of_date)
        )
    elif status:
        stmt = stmt.where(Report.status == status)
        count_stmt = count_stmt.where(Report.status == status)
    elif not include_retired:
        stmt = stmt.where(Report.status == ReportStatus.ACTIVE.value)
        count_stmt = count_stmt.where(Report.status == ReportStatus.ACTIVE.value)

    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(Report.code, Report.valid_from.desc()).limit(limit).offset(offset)
    ).all()
    lineage_map = lineage_index(db)
    return {
        "items": [serialize(row, lineage=lineage_map.get(row.code, [])) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "as_of": as_of_date.isoformat() if as_of_date else None,
    }


def get_report(db: Session, report_id: int) -> Report:
    report = db.get(Report, report_id)
    if report is None:
        from src.utils.errors import not_found

        raise not_found("report", report_id)
    return report


def active_report_map(db: Session, *, as_of: date | None = None) -> dict[str, Report]:
    """code -> 时点有效登记（每个 code 至多一条）。"""
    stmt = select(Report)
    if as_of is None:
        stmt = stmt.where(Report.status == ReportStatus.ACTIVE.value)
    else:
        stmt = stmt.where(Report.valid_from <= as_of).where(
            (Report.valid_to.is_(None)) | (Report.valid_to > as_of)
        )
    result: dict[str, Report] = {}
    for report in db.scalars(stmt.order_by(Report.code, Report.valid_from.desc())).all():
        result.setdefault(report.code, report)
    return result


def reports_by_metric(db: Session, *, as_of: date | str | None = None) -> dict[str, list[dict]]:
    """指标 -> 时点有效下游报表的反向索引（算法 B 唯一报表入口）。"""
    as_of_date = _coerce_as_of(as_of)
    index: dict[str, list[dict]] = {}
    for report in active_report_map(db, as_of=as_of_date).values():
        weight = report.weight or (REPORT_WEIGHT_CORE if report.is_core else REPORT_WEIGHT_NORMAL)
        entry = {
            "code": report.code,
            "name": report.name,
            "is_core": bool(report.is_core),
            "weight": int(weight),
            "owner": report.owner,
            "channel": report.channel,
        }
        for metric_code in report.metric_codes or []:
            index.setdefault(metric_code, []).append(entry)
    return index


def count_reports(db: Session, *, active_only: bool = True) -> int:
    stmt = select(func.count()).select_from(Report)
    if active_only:
        stmt = stmt.where(Report.status == ReportStatus.ACTIVE.value)
    return db.scalar(stmt) or 0


# --------------------------------------------------------------------------- #
# 血缘
# --------------------------------------------------------------------------- #
def _serialize_lineage(row: ReportLineage) -> dict:
    return {
        "id": row.id,
        "relation_kind": row.relation_kind,
        "relation_kind_label": REPORT_LINEAGE_KIND_LABELS.get(row.relation_kind, row.relation_kind),
        "source_code": row.source_code,
        "target_code": row.target_code,
        "effective_on": row.effective_on.isoformat() if row.effective_on else None,
        "note": row.note or "",
        "batch_no": row.batch_no,
        "created_at": iso_utc(row.created_at),
    }


def lineage_index(db: Session) -> dict[str, list[dict]]:
    rows = db.scalars(select(ReportLineage).order_by(ReportLineage.effective_on, ReportLineage.id)).all()
    index: dict[str, list[dict]] = {}
    for row in rows:
        payload = _serialize_lineage(row)
        index.setdefault(row.source_code, []).append(payload)
        if row.target_code:
            index.setdefault(row.target_code, []).append(payload)
    return index


def list_lineage(db: Session, *, code: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    stmt = select(ReportLineage)
    count_stmt = select(func.count()).select_from(ReportLineage)
    if code:
        condition = (ReportLineage.source_code == code) | (ReportLineage.target_code == code)
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(ReportLineage.effective_on.desc(), ReportLineage.id.desc()).limit(limit).offset(offset)
    ).all()
    return {"items": [_serialize_lineage(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def _add_lineage(
    db: Session,
    *,
    relation_kind: str,
    source_code: str,
    target_code: str | None,
    effective_on: date,
    note: str,
    batch_no: str | None,
) -> bool:
    """追加一条血缘（自然键去重），返回是否新写入。"""
    exists = db.scalar(
        select(ReportLineage).where(
            ReportLineage.relation_kind == relation_kind,
            ReportLineage.source_code == source_code,
            ReportLineage.target_code == target_code,
            ReportLineage.effective_on == effective_on,
        )
    )
    if exists is not None:
        return False
    db.add(
        ReportLineage(
            relation_kind=relation_kind,
            source_code=source_code,
            target_code=target_code,
            effective_on=effective_on,
            note=note or "",
            batch_no=batch_no,
        )
    )
    return True


# --------------------------------------------------------------------------- #
# 清单导入（批次幂等 + 停用不可复活）
# --------------------------------------------------------------------------- #
MUTABLE_FIELDS = ("name", "owner", "channel", "is_core", "weight", "metric_codes", "refresh_cron")


def _retire_report(db: Session, report: Report, *, valid_to: date, note: str, batch_no: str | None) -> None:
    report.status = ReportStatus.RETIRED.value
    report.valid_to = valid_to
    _add_lineage(
        db,
        relation_kind=ReportLineageKind.RETIRE.value,
        source_code=report.code,
        target_code=None,
        effective_on=valid_to,
        note=note or f"{report.code} 于 {valid_to} 停用",
        batch_no=batch_no,
    )


def import_batch(db: Session, payload: dict, *, operator_email: str) -> dict:
    """按批次导入清单。同一 batch_no 重跑不新增记录；retired 登记不复活。"""
    batch_no = (payload.get("batch_no") or "").strip()
    if not batch_no:
        raise AppError("VALIDATION", 422, {"field": "batch_no"})
    rows = payload.get("reports") or []
    if not isinstance(rows, list) or not rows:
        raise AppError("VALIDATION", 422, {"field": "reports", "message": "reports 不能为空"})

    existing_batch = db.scalar(select(ReportImportBatch).where(ReportImportBatch.batch_no == batch_no))
    if existing_batch is not None:
        # 幂等：重跑同一批次直接回放首次结果，绝不新增/改写任何记录。
        return _batch_result(existing_batch, replayed=True)

    inserted = updated = skipped = lineage_added = 0
    details: list[dict] = []
    default_from = _coerce_valid_date(payload.get("valid_from"), field="valid_from", default=EPOCH_DATE)

    for raw in rows:
        code = (raw.get("code") or "").strip()
        if not code:
            raise AppError("VALIDATION", 422, {"field": "reports[].code"})
        valid_from = _coerce_valid_date(raw.get("valid_from"), field="valid_from", default=default_from)
        valid_to = _coerce_valid_date(raw.get("valid_to"), field="valid_to", default=None)
        row_status = (raw.get("status") or ReportStatus.ACTIVE.value).strip()
        if row_status not in (ReportStatus.ACTIVE.value, ReportStatus.RETIRED.value):
            raise AppError("VALIDATION", 422, {"field": "reports[].status", "code": code})
        if valid_to is not None and valid_to <= valid_from:
            raise AppError(
                "VALIDATION", 422, {"field": "valid_to", "code": code, "message": "valid_to 必须晚于 valid_from"}
            )

        active = db.scalar(
            select(Report).where(Report.code == code, Report.status == ReportStatus.ACTIVE.value)
        )
        retired = db.scalar(
            select(Report).where(Report.code == code, Report.status == ReportStatus.RETIRED.value)
        )

        # 规则 1：已停用的报表编码不能被晚到/乱序记录恢复成当前有效。
        if row_status == ReportStatus.ACTIVE.value and active is None and retired is not None:
            skipped += 1
            details.append({"code": code, "action": "skipped", "reason": "retired_code_not_revived"})
            continue

        if active is not None:
            if row_status == ReportStatus.RETIRED.value:
                retire_on = valid_to or local_now().date()
                _retire_report(
                    db, active, valid_to=retire_on, note=raw.get("note") or "", batch_no=batch_no
                )
                lineage_added += 1
                updated += 1
                details.append({"code": code, "action": "retired", "valid_to": retire_on.isoformat()})
                continue
            # active -> active：只更新可变属性；valid_from 不可变，历史不被改写。
            for field in MUTABLE_FIELDS:
                if field in raw and raw[field] is not None:
                    setattr(active, field, raw[field])
            if valid_to is not None:
                active.valid_to = valid_to
            updated += 1
            details.append({"code": code, "action": "updated"})
            continue

        # 全新 code：落一条新登记（retired 历史登记也允许直接导入，用于补历史）。
        db.add(
            Report(
                code=code,
                name=(raw.get("name") or code).strip(),
                owner=raw.get("owner") or "星野零售 · 指标体系组",
                channel=raw.get("channel") or "BI",
                is_core=bool(raw.get("is_core", False)),
                weight=int(raw.get("weight") or (REPORT_WEIGHT_CORE if raw.get("is_core") else REPORT_WEIGHT_NORMAL)),
                metric_codes=list(raw.get("metric_codes") or []),
                refresh_cron=raw.get("refresh_cron") or "0 7 * * *",
                valid_from=valid_from,
                valid_to=valid_to,
                status=row_status,
            )
        )
        inserted += 1
        details.append({"code": code, "action": "inserted", "valid_from": valid_from.isoformat()})

    # 拆分 / 合并 / 改名血缘（随批次一起幂等写入）。
    for edge in payload.get("lineage") or []:
        kind = (edge.get("relation_kind") or "").strip()
        if kind not in {item.value for item in ReportLineageKind}:
            raise AppError("VALIDATION", 422, {"field": "lineage[].relation_kind"})
        source = (edge.get("source_code") or "").strip()
        target = (edge.get("target_code") or None)
        effective_on = _coerce_valid_date(edge.get("effective_on"), field="effective_on", default=local_now().date())
        if _add_lineage(
            db,
            relation_kind=kind,
            source_code=source,
            target_code=target,
            effective_on=effective_on,
            note=edge.get("note") or "",
            batch_no=batch_no,
        ):
            lineage_added += 1

    db.flush()
    batch = ReportImportBatch(
        batch_no=batch_no,
        source=(payload.get("source") or "manual").strip(),
        status="completed",
        row_count=len(rows),
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        lineage_added=lineage_added,
        detail={"rows": details},
        operator_email=operator_email,
    )
    db.add(batch)
    db.flush()
    return _batch_result(batch, replayed=False)


def _batch_result(batch: ReportImportBatch, *, replayed: bool) -> dict:
    return {
        "batch_no": batch.batch_no,
        "source": batch.source,
        "status": batch.status,
        "replayed": replayed,
        "row_count": batch.row_count,
        "inserted": batch.inserted,
        "updated": batch.updated,
        "skipped": batch.skipped,
        "lineage_added": batch.lineage_added,
        "detail": batch.detail or {},
        "created_at": iso_utc(batch.created_at),
    }


def list_batches(db: Session, *, limit: int = 50, offset: int = 0) -> dict:
    total = db.scalar(select(func.count()).select_from(ReportImportBatch)) or 0
    rows = db.scalars(
        select(ReportImportBatch).order_by(ReportImportBatch.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "batch_no": row.batch_no,
                "source": row.source,
                "status": row.status,
                "row_count": row.row_count,
                "inserted": row.inserted,
                "updated": row.updated,
                "skipped": row.skipped,
                "lineage_added": row.lineage_added,
                "operator_email": row.operator_email,
                "created_at": iso_utc(row.created_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def now_date() -> date:
    return local_now().date()
