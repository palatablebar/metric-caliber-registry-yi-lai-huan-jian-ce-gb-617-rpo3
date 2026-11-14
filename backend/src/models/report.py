"""报表登记：指标被哪些 BI 报表消费；核心报表权重 ×3，参与影响面打分。

一个报表编码（code）可能因为改名、拆分、合并而多次出现，因此每条登记都带
生效期间（valid_from / valid_to）与状态（active/retired）：
- 同一 code 只有一条有效登记（active），历史登记保留为 retired，绝不原地改写；
- 拆分 / 合并的前后归属落在 ReportLineage 上；
- 清单导入按批次（ReportImportBatch）幂等，且迟到/乱序记录不能把已停用报表复活。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("code", "valid_from", name="uq_reports_code_valid_from"),
        Index("ix_reports_code_active", "code", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(24), index=True)
    name: Mapped[str] = mapped_column(String(120))
    owner: Mapped[str] = mapped_column(String(80), default="星野零售 · 指标体系组")
    channel: Mapped[str] = mapped_column(String(40), default="BI")
    is_core: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    metric_codes: Mapped[list] = mapped_column(JSON, default=list)
    refresh_cron: Mapped[str] = mapped_column(String(40), default="0 7 * * *")
    # 生效期间：[valid_from, valid_to)，valid_to 为空表示至今有效。
    valid_from: Mapped[date] = mapped_column(Date, default=date.min)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    # active 生效中 / retired 已停用（改名、拆分、合并、下线后的历史登记）。
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReportLineage(Base):
    """报表编码血缘：拆分 / 合并 / 改名 / 停用的前后归属，只追加不改写。"""

    __tablename__ = "report_lineages"
    __table_args__ = (
        UniqueConstraint(
            "relation_kind",
            "source_code",
            "target_code",
            "effective_on",
            name="uq_report_lineage_edge",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # rename 改名 / split 拆分 / merge 合并 / retire 停用
    relation_kind: Mapped[str] = mapped_column(String(16), index=True)
    source_code: Mapped[str] = mapped_column(String(24), index=True)
    target_code: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    effective_on: Mapped[date] = mapped_column(Date, index=True)
    note: Mapped[str] = mapped_column(String(400), default="")
    batch_no: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReportImportBatch(Base):
    """清单导入批次：同批次重跑不新增记录（幂等键 batch_no）。"""

    __tablename__ = "report_import_batches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(80), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="completed")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    lineage_added: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
