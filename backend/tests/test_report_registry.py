"""报表生效期间 / 血缘 / 清单导入的集成测试（SQLite 内存库，不依赖外部 PG）。

覆盖需求：
1. 同一 code 改名/拆分/合并后新旧编号共存，当前有效去重（核心报表不被重复计入）；
2. as_of 时点还原：指标详情输入、影响面、冲突按同一时点看到同一套上下游；
3. 清单批次幂等：重跑同一 batch_no 不新增记录；
4. 停用不可复活：晚到/乱序的 active 记录不能把 retired 报表恢复成当前有效；
5. 历史血缘只追加，历史变更不被今天的映射改写。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.constants.enums import ReportStatus
from src.models import Base
from src.models.dependency import Dependency
from src.models.report import Report, ReportImportBatch, ReportLineage
from src.services import graph_service, report_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_report(db, code, *, valid_from, valid_to=None, status=ReportStatus.ACTIVE.value,
                is_core=False, weight=None, metric_codes=()):
    db.add(
        Report(
            code=code,
            name=f"报表{code}",
            owner="测试",
            channel="BI",
            is_core=is_core,
            weight=weight if weight is not None else (3 if is_core else 1),
            metric_codes=list(metric_codes),
            valid_from=valid_from,
            valid_to=valid_to,
            status=status,
        )
    )
    db.flush()


# --------------------------------------------------------------------------- #
# 时点还原
# --------------------------------------------------------------------------- #
def test_effective_period_filtering(db):
    _add_report(db, "r00", valid_from=date(2024, 1, 1), valid_to=date(2025, 9, 1),
                status=ReportStatus.RETIRED.value, is_core=True, metric_codes=["gmv"])
    _add_report(db, "r01", valid_from=date(2025, 9, 1), is_core=True, metric_codes=["gmv"])

    # 当前（无 as_of）：只看到 active 的 r01
    current = report_service.reports_by_metric(db)
    assert [r["code"] for r in current["gmv"]] == ["r01"]

    # 旧时点：只看到 r00
    old = report_service.reports_by_metric(db, as_of=date(2025, 8, 31))
    assert [r["code"] for r in old["gmv"]] == ["r00"]

    # 切换日当天：[valid_from, valid_to) 语义，r01 生效、r00 已失效
    switch = report_service.reports_by_metric(db, as_of=date(2025, 9, 1))
    assert [r["code"] for r in switch["gmv"]] == ["r01"]


def test_renamed_core_report_not_double_counted(db):
    # r00 -> r01 改名，都是核心；若新旧同时计入，gmv 的影响分会被翻倍。
    _add_report(db, "r00", valid_from=date(2024, 1, 1), valid_to=date(2025, 9, 1),
                status=ReportStatus.RETIRED.value, is_core=True, metric_codes=["gmv"])
    _add_report(db, "r01", valid_from=date(2025, 9, 1), is_core=True, metric_codes=["gmv"])

    current = report_service.reports_by_metric(db)
    assert len(current["gmv"]) == 1
    assert report_service.active_report_map(db)["r01"].weight == 3


def test_as_of_validation(db):
    with pytest.raises(Exception):
        report_service.reports_by_metric(db, as_of="not-a-date")


def test_list_reports_as_of_and_status(db):
    _add_report(db, "r00", valid_from=date(2024, 1, 1), valid_to=date(2025, 9, 1),
                status=ReportStatus.RETIRED.value)
    _add_report(db, "r01", valid_from=date(2025, 9, 1))

    default_list = report_service.list_reports(db)
    assert {item["code"] for item in default_list["items"]} == {"r01"}

    retired_list = report_service.list_reports(db, status=ReportStatus.RETIRED.value)
    assert {item["code"] for item in retired_list["items"]} == {"r00"}

    historical = report_service.list_reports(db, as_of=date(2025, 1, 1))
    assert {item["code"] for item in historical["items"]} == {"r00"}

    all_rows = report_service.list_reports(db, include_retired=True, limit=200)
    assert {item["code"] for item in all_rows["items"]} == {"r00", "r01"}


# --------------------------------------------------------------------------- #
# 批次幂等 + 停用不可复活
# --------------------------------------------------------------------------- #
def _payload(batch_no="batch-001", rows=None, lineage=None, valid_from="2026-01-01"):
    return {
        "batch_no": batch_no,
        "source": "unit-test",
        "valid_from": valid_from,
        "reports": rows
        or [
            {"code": "r10", "name": "财务成本分析", "is_core": False, "metric_codes": ["cost_total"]},
        ],
        "lineage": lineage or [],
    }


def test_import_batch_is_idempotent(db):
    first = report_service.import_batch(db, _payload(), operator_email="admin@x.io")
    assert first["inserted"] == 1 and first["replayed"] is False
    assert db.scalar(select(func.count()).select_from(Report)) == 1

    second = report_service.import_batch(db, _payload(), operator_email="admin@x.io")
    assert second["replayed"] is True
    assert second["inserted"] == 1  # 回放首次结果
    assert db.scalar(select(func.count()).select_from(Report)) == 1
    assert db.scalar(select(func.count()).select_from(ReportImportBatch)) == 1


def test_retired_report_cannot_be_revived_by_late_row(db):
    # 第一批：导入并停用 r10
    report_service.import_batch(
        db,
        _payload(
            "batch-retire",
            rows=[{"code": "r10", "name": "成本分析", "status": "retired",
                   "valid_from": "2025-01-01", "valid_to": "2026-03-01"}],
        ),
        operator_email="admin@x.io",
    )
    # 乱序/晚到的第二批试图把 r10 以 active 重新登记
    result = report_service.import_batch(
        db,
        _payload(
            "batch-late",
            rows=[{"code": "r10", "name": "成本分析复活", "status": "active"}],
        ),
        operator_email="admin@x.io",
    )
    assert result["skipped"] == 1 and result["inserted"] == 0
    report = db.scalar(select(Report).where(Report.code == "r10"))
    assert report.status == ReportStatus.RETIRED.value
    assert report.name == "成本分析"  # 没有被晚到记录改写
    assert report_service.count_reports(db, active_only=True) == 0


def test_active_report_update_keeps_valid_from(db):
    report_service.import_batch(
        db, _payload("batch-a", valid_from="2026-01-01"), operator_email="admin@x.io"
    )
    report_service.import_batch(
        db,
        _payload(
            "batch-b",
            valid_from="2026-06-01",  # 试图改生效日
            rows=[{"code": "r10", "name": "财务成本分析v2", "metric_codes": ["cost_total", "finance_tax"]}],
        ),
        operator_email="admin@x.io",
    )
    report = db.scalar(select(Report).where(Report.code == "r10"))
    assert report.valid_from == date(2026, 1, 1)  # 生效日不可变
    assert report.name == "财务成本分析v2"
    assert report.metric_codes == ["cost_total", "finance_tax"]


def test_retire_active_report_writes_lineage(db):
    report_service.import_batch(db, _payload("batch-on"), operator_email="admin@x.io")
    result = report_service.import_batch(
        db,
        _payload(
            "batch-off",
            rows=[{"code": "r10", "name": "财务成本分析", "status": "retired", "valid_to": "2026-09-01"}],
        ),
        operator_email="admin@x.io",
    )
    assert result["updated"] == 1 and result["lineage_added"] == 1
    report = db.scalar(select(Report).where(Report.code == "r10"))
    assert report.status == ReportStatus.RETIRED.value
    assert report.valid_to == date(2026, 9, 1)
    lineage = db.scalars(select(ReportLineage).where(ReportLineage.source_code == "r10")).all()
    assert len(lineage) == 1 and lineage[0].relation_kind == "retire"


def test_split_merge_lineage_appended_not_rewritten(db):
    report_service.import_batch(
        db,
        _payload(
            "batch-split",
            rows=[
                {"code": "r20", "name": "旧周报", "status": "retired",
                 "valid_from": "2024-06-01", "valid_to": "2026-01-01"},
                {"code": "r06", "name": "会员月报", "valid_from": "2026-01-01"},
                {"code": "r07", "name": "流量日报", "valid_from": "2026-01-01"},
            ],
            lineage=[
                {"relation_kind": "split", "source_code": "r20", "target_code": "r06",
                 "effective_on": "2026-01-01", "note": "拆分"},
                {"relation_kind": "split", "source_code": "r20", "target_code": "r07",
                 "effective_on": "2026-01-01", "note": "拆分"},
            ],
        ),
        operator_email="admin@x.io",
    )
    # 重跑同批次：血缘不重复
    report_service.import_batch(
        db,
        _payload(
            "batch-split",
            rows=[{"code": "r06", "name": "会员月报"}],
            lineage=[
                {"relation_kind": "split", "source_code": "r20", "target_code": "r06",
                 "effective_on": "2026-01-01", "note": "拆分"},
            ],
        ),
        operator_email="admin@x.io",
    )
    edges = db.scalars(select(ReportLineage)).all()
    assert len(edges) == 2  # 没有重复追加
    lineage = report_service.list_lineage(db, code="r20")
    assert {item["target_code"] for item in lineage["items"]} == {"r06", "r07"}


def test_invalid_valid_to_rejected(db):
    with pytest.raises(Exception):
        report_service.import_batch(
            db,
            _payload(
                "bad",
                rows=[{"code": "r99", "name": "x", "valid_from": "2026-05-01",
                       "valid_to": "2026-04-01", "status": "retired"}],
            ),
            operator_email="admin@x.io",
        )
    db.rollback()


# --------------------------------------------------------------------------- #
# 与算法 B 的端到端衔接
# --------------------------------------------------------------------------- #
def test_collect_inputs_uses_point_in_time_reports(db):
    # 无指标时只验证报表索引装配不报错且按时点过滤
    _add_report(db, "r00", valid_from=date(2024, 1, 1), valid_to=date(2025, 9, 1),
                status=ReportStatus.RETIRED.value, metric_codes=["gmv"])
    _add_report(db, "r01", valid_from=date(2025, 9, 1), metric_codes=["gmv"])
    _, current_index, _ = graph_service.collect_inputs(db)
    _, old_index, _ = graph_service.collect_inputs(db, as_of=date(2025, 1, 1))
    assert list(current_index.get("gmv", []))[0]["code"] == "r01"
    assert list(old_index.get("gmv", []))[0]["code"] == "r00"


# --------------------------------------------------------------------------- #
# 端到端：真实指标 + 公式，影响面/冲突按时点还原且同点一致
# --------------------------------------------------------------------------- #
def _make_metric_with_formula(db, code, name, dsl):
    from src.services import formula_service, metric_service

    metric = metric_service.create_metric(
        db,
        {"code": code, "name": name, "domain": "trade", "tier": "normal", "unit": "元"},
        author="admin@x.io",
    )
    formula_service.save_version(db, metric, dsl_text=dsl, author_email="admin@x.io")
    return metric


def test_impact_and_conflict_restore_by_point_in_time(db):
    _make_metric_with_formula(
        db, "gmv", "GMV", "gmv = sum(dwd.order.amount) where order_status = 'paid' group by dt"
    )
    _make_metric_with_formula(
        db,
        "gmv_net",
        "GMV",
        "gmv_net = sum(dwd.order.amount) - sum(dwd.refund.amount) "
        "where order_status = 'paid' group by dt",
    )

    # 历史时点：gmv 只被一张普通报表 r_old 引用
    _add_report(db, "r_old", valid_from=date(2024, 1, 1), valid_to=date(2025, 9, 1),
                status=ReportStatus.RETIRED.value, is_core=False, weight=1, metric_codes=["gmv"])
    # 当前：gmv / gmv_net 都进核心报表 r_new（权重 3）
    _add_report(db, "r_new", valid_from=date(2025, 9, 1), is_core=True, weight=3,
                metric_codes=["gmv", "gmv_net"])

    # 当前影响面：核心报表 1 张 -> 3 分
    current = graph_service.impact_for_metric(db, "gmv")
    assert current["reports"] == ["r_new"] and current["score"] == 3

    # 历史影响面：普通报表 1 张 -> 1 分；同一指标不同时点分数不同但同点稳定
    historical = graph_service.impact_for_metric(db, "gmv", as_of=date(2025, 1, 1))
    assert historical["reports"] == ["r_old"] and historical["score"] == 1
    historical_again = graph_service.impact_for_metric(db, "gmv", as_of=date(2025, 1, 1))
    assert historical == historical_again

    # 冲突在两个时点都检出，但影响面随报表期间变化
    conflicts_now = graph_service.find_conflicts(db)
    conflicts_then = graph_service.find_conflicts(db, as_of=date(2025, 1, 1))
    assert len(conflicts_now) == 1 and len(conflicts_then) == 1
    assert conflicts_now[0]["score"] == 3
    assert conflicts_then[0]["score"] == 1


def test_change_request_snapshot_records_as_of(db, monkeypatch):
    from src.services import change_service

    _make_metric_with_formula(
        db, "gmv", "GMV", "gmv = sum(dwd.order.amount) where order_status = 'paid' group by dt"
    )
    _add_report(db, "r_new", valid_from=date(2025, 9, 1), is_core=True, weight=3, metric_codes=["gmv"])

    # 变更单创建时把影响面快照（含 as_of）固化，后续报表变动不改写历史快照
    change = change_service.create_change(
        db,
        {"change_kind": "caliber", "target_ref": "gmv", "metric_code": "gmv", "reason": "月会口径"},
        requester="analyst@x.io",
    )
    assert change.impact["score"] == 3
    assert change.impact["as_of"] is None  # 当前快照

    # 再停用报表，历史变更单的分数不被改写
    report = db.scalar(select(Report).where(Report.code == "r_new"))
    report.status = ReportStatus.RETIRED.value
    report.valid_to = date(2026, 1, 1)
    db.flush()
    stored = db.scalar(select(type(change)).where(type(change).id == change.id))
    assert stored.impact["score"] == 3
    # 实时计算已经反映停用
    assert graph_service.impact_for_metric(db, "gmv")["report_count"] == 0


def test_dependency_rebuild_ignores_retired_reports(db):
    from src.services import dependency_service, dsl_parser, formula_service, metric_service

    metric = metric_service.create_metric(
        db, {"code": "gmv", "name": "GMV", "domain": "trade", "tier": "normal", "unit": "元"},
        author="admin@x.io",
    )
    formula = formula_service.save_version(
        db, metric,
        dsl_text="gmv = sum(dwd.order.amount) where order_status = 'paid' group by dt",
        author_email="admin@x.io",
    )
    parsed = dsl_parser.parse_dsl(formula.dsl_text, metric.code)
    _add_report(db, "r_dead", valid_from=date(2024, 1, 1), valid_to=date(2025, 1, 1),
                status=ReportStatus.RETIRED.value, metric_codes=["gmv"])
    _add_report(db, "r_live", valid_from=date(2025, 1, 1), metric_codes=["gmv"])

    stats = dependency_service.rebuild(db, [metric], {"gmv": parsed})
    report_edges = [
        edge for edge in db.scalars(select(Dependency)).all()
        if edge.kind == "report"
    ]
    assert {edge.to_ref for edge in report_edges} == {"r_live"}
    assert stats["counts"]["report"] == 1

