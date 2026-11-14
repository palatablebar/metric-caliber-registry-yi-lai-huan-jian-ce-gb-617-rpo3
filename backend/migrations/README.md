# migrations

参考建表语句。运行时由 SQLAlchemy 的 `Base.metadata.create_all` 建表（启动时执行），
本目录的 SQL 只用于评审时快速看清表结构与自然键/唯一约束，**不会被执行**。

- 5 个业务实体：`metrics` / `formulas` / `dependencies` / `reports` / `change_requests`
- 报表生效期间三件套：`reports`（valid_from/valid_to/status）/ `report_lineages`（改名/拆分/合并/停用血缘）
  / `report_import_batches`（清单导入批次，batch_no 幂等）
- 横切：`users`（三角色）、`audit_logs`（审计）、`runs`（校验运行）
- 自然键：`metrics.code`、`formulas(metric_id, version)`、`dependencies(from_metric_id, kind, to_ref)`、
  `reports(code, valid_from)`、`report_lineages(relation_kind, source_code, target_code, effective_on)`、
  `report_import_batches.batch_no`、`change_requests.cr_no`、`runs.run_no`

## 报表编码生效期间规则

- 一条登记在时点 `as_of` 有效 ⇔ `valid_from <= as_of` 且（`valid_to` 为空或 `as_of < valid_to`）；
- 同一 `code` 至多一条 `active`；改名/拆分/合并不原地改写，旧登记置 `retired`（写 `valid_to`），新登记另起；
- 前后归属只追加到 `report_lineages`，历史变更单的影响面快照不会被今天的映射改写；
- 清单导入按 `batch_no` 幂等（重跑回放首次结果），且 `retired` 编码不会被晚到/乱序记录复活。

