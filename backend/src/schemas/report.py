from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

REPORT_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,23}$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


class ReportImportRow(BaseModel):
    code: str = Field(min_length=2, max_length=24, pattern=REPORT_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    owner: str | None = Field(default=None, max_length=80)
    channel: str | None = Field(default=None, max_length=40)
    is_core: bool = False
    weight: int | None = Field(default=None, ge=1, le=10)
    metric_codes: list[str] = Field(default_factory=list)
    refresh_cron: str | None = Field(default=None, max_length=40)
    valid_from: str | None = Field(default=None, pattern=DATE_PATTERN)
    valid_to: str | None = Field(default=None, pattern=DATE_PATTERN)
    status: Literal["active", "retired"] = "active"
    note: str | None = Field(default=None, max_length=400)


class ReportLineageIn(BaseModel):
    relation_kind: Literal["rename", "split", "merge", "retire"]
    source_code: str = Field(min_length=2, max_length=24)
    target_code: str | None = Field(default=None, max_length=24)
    effective_on: str | None = Field(default=None, pattern=DATE_PATTERN)
    note: str | None = Field(default=None, max_length=400)


class ReportImportIn(BaseModel):
    batch_no: str = Field(min_length=4, max_length=64)
    source: str = Field(default="manual", max_length=80)
    valid_from: str | None = Field(default=None, pattern=DATE_PATTERN)
    reports: list[ReportImportRow] = Field(min_length=1, max_length=2000)
    lineage: list[ReportLineageIn] = Field(default_factory=list)
