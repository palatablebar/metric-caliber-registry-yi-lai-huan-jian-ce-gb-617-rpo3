import { api } from '../api.js';
import { ENUMS, can, esc } from '../config.js';
import { drawer, emptyState, errorBox, errorMessage, field, filterBar, kv, table, toast } from '../ui.js';

const state = { status: '', asOf: '', includeRetired: false, q: '' };

function statusBadge(status) {
  const tone = status === 'active' ? 'ok' : 'danger';
  return `<span class="badge ${tone}">${esc(ENUMS.reportStatus[status] || status)}</span>`;
}

function openReport(view, ctx, report) {
  const lineage = report.lineage || [];
  drawer({
    title: `${report.name} · ${report.code}`,
    subtitle: `生效期间 ${report.valid_from || '—'} ~ ${report.valid_to || '至今'} · ${esc(ENUMS.reportStatus[report.status] || report.status)}`,
    body: `
      ${kv([
        ['报表编码', `<span class="mono">${esc(report.code)}</span>`],
        ['状态', statusBadge(report.status)],
        ['渠道', esc(report.channel)],
        ['负责人', esc(report.owner)],
        ['分级', report.is_core ? '<span class="badge high">核心 ×3</span>' : '普通 ×1'],
        ['刷新周期', `<span class="mono">${esc(report.refresh_cron)}</span>`],
        ['生效起', esc(report.valid_from || '—')],
        ['生效止', esc(report.valid_to || '至今（仍有效）')],
      ])}
      <div class="dsl-box">
        <span class="label">引用指标（${report.metric_count}）</span>
        <p class="mono tiny">${esc((report.metric_codes || []).join(' / ') || '—')}</p>
      </div>
      <div class="dsl-box">
        <span class="label">拆分 / 合并 / 改名前后归属</span>
        ${
          lineage.length
            ? `<div class="list-tight">${lineage
                .map(
                  (edge) => `<div class="impact-row">
                    <span class="badge info">${esc(ENUMS.reportLineageKind[edge.relation_kind] || edge.relation_kind)}</span>
                    <span class="mono tiny">${esc(edge.source_code)} → ${esc(edge.target_code || '—')} @ ${esc(edge.effective_on)}</span>
                  </div><p class="section-note tiny">${esc(edge.note || '')}</p>`,
                )
                .join('')}</div>`
            : '<p class="muted tiny">没有血缘记录。</p>'
        }
      </div>`,
  });
}

function openImport(view, ctx) {
  const example = {
    batch_no: 'batch-YYYYMMDD-001',
    source: 'bi-manual',
    reports: [
      {
        code: 'r13',
        name: '新报表名称',
        is_core: false,
        metric_codes: ['gmv'],
        valid_from: '2026-11-14',
        status: 'active',
      },
    ],
    lineage: [
      { relation_kind: 'split', source_code: 'r20', target_code: 'r13', effective_on: '2026-11-14', note: '拆分明细' },
    ],
  };
  const { root, close } = drawer({
    title: '导入报表清单（批次幂等）',
    subtitle: '同 batch_no 重跑不新增；已停用编号不会被晚到记录复活',
    body: `
      <div class="dsl-box">
        <span class="label">清单 JSON</span>
        <textarea id="import-json" class="code" rows="16" style="width:100%;font-family:monospace">${esc(JSON.stringify(example, null, 2))}</textarea>
      </div>
      <p class="section-note tiny">规则：active 登记只更新可变属性、valid_from 不可变；status=retired 需带 valid_to；lineage 为可选血缘。</p>
      <button class="btn sm accent" id="submit-import" type="button">提交批次</button>
      <span id="import-result" class="tiny mono"></span>`,
  });
  root.querySelector('#submit-import')?.addEventListener('click', async () => {
    let payload;
    try {
      payload = JSON.parse(root.querySelector('#import-json').value);
    } catch {
      toast('JSON 格式不合法', 'err');
      return;
    }
    try {
      const result = await api.post('/api/reports/imports', payload);
      const flag = result.replayed ? '（重跑回放，未新增）' : '';
      toast(
        `批次 ${result.batch_no}：新增 ${result.inserted} / 更新 ${result.updated} / 跳过 ${result.skipped} / 血缘 ${result.lineage_added} ${flag}`,
      );
      close();
      await reportsPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

async function renderLineage(view) {
  const panel = view.querySelector('#lineage-panel');
  if (!panel) return;
  try {
    const data = await api.get('/api/reports/lineages', { limit: 50 });
    panel.innerHTML = `<div class="panel-head"><h2>编码血缘 · 拆分/合并/改名/停用</h2><span class="meta">${data.total} 条 · 只追加</span></div>
      <div class="panel-body flush">${table({
        rows: data.items,
        columns: [
          { key: 'effective_on', label: '生效日', className: 'mono' },
          {
            key: 'relation_kind',
            label: '类型',
            render: (row) => `<span class="badge info">${esc(ENUMS.reportLineageKind[row.relation_kind] || row.relation_kind)}</span>`,
          },
          { key: 'source_code', label: '原编号', className: 'mono' },
          { key: 'target_code', label: '后继编号', className: 'mono', render: (row) => esc(row.target_code || '—（停用）') },
          { key: 'note', label: '说明', render: (row) => `<span class="tiny">${esc(row.note || '')}</span>` },
        ],
        empty: { title: '没有血缘记录', hint: '导入清单时带 lineage 即会登记。' },
      })}</div>`;
  } catch (err) {
    panel.innerHTML = `<div class="panel-body"><div class="error-box"><span>${esc(errorMessage(err))}</span></div></div>`;
  }
}

async function renderBatches(view) {
  const panel = view.querySelector('#batch-panel');
  if (!panel) return;
  try {
    const data = await api.get('/api/reports/import-batches', { limit: 20 });
    panel.innerHTML = `<div class="panel-head"><h2>清单导入批次</h2><span class="meta">${data.total} 个 · 幂等</span></div>
      <div class="panel-body flush">${table({
        rows: data.items,
        columns: [
          { key: 'batch_no', label: '批次号', className: 'mono' },
          { key: 'source', label: '来源' },
          { key: 'row_count', label: '行数', className: 'num' },
          { key: 'inserted', label: '新增', className: 'num' },
          { key: 'updated', label: '更新', className: 'num' },
          { key: 'skipped', label: '跳过', className: 'num' },
          { key: 'lineage_added', label: '血缘', className: 'num' },
        ],
        empty: { title: '还没有导入批次', hint: '管理员可在上方提交清单。' },
      })}</div>`;
  } catch (err) {
    panel.innerHTML = `<div class="panel-body"><div class="error-box"><span>${esc(errorMessage(err))}</span></div></div>`;
  }
}

export const reportsPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.get('/api/reports', {
        status: state.status || undefined,
        as_of: state.asOf || undefined,
        include_retired: state.includeRetired || undefined,
        q: state.q || undefined,
        limit: 200,
      });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => reportsPage.render(view, ctx));
      return;
    }
    const writable = can(ctx.user.role, 'report:write');
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>报表编码登记 · 生效期间</h2>
          <div style="display:flex;gap:10px;align-items:center">
            <span class="meta">${data.total} 条${data.as_of ? ` · 还原时点 ${esc(data.as_of)}` : ' · 当前有效'}</span>
            ${writable ? '<button class="btn sm accent" id="open-import">导入清单</button>' : ''}
            ${can(ctx.user.role, 'export:read') ? '<button class="btn sm ghost" id="export-reports">导出 CSV</button>' : ''}
          </div>
        </div>
        ${filterBar('report-filters', [
          {
            label: '状态', name: 'status', value: state.status,
            options: [
              { value: '', label: '当前有效' },
              { value: 'active', label: '生效中' },
              { value: 'retired', label: '已停用' },
            ],
          },
          { label: '还原时点 as_of', name: 'as_of', value: state.asOf, placeholder: 'YYYY-MM-DD', type: 'date' },
        ])}
        <p class="section-note tiny" style="padding:0 16px">
          提示：填入 as_of 后按该时点的生效期间还原（自动忽略状态筛选）；清空时点并选「已停用」可查看历史登记。
        </p>
        ${table({
          rows: data.items,
          columns: [
            { key: 'code', label: '索引号', className: 'mono', render: (row) => `<span class="index-tag">${esc(row.code)}</span>` },
            { key: 'name', label: '报表名称', render: (row) => `<b>${esc(row.name)}</b>` },
            { key: 'status', label: '状态', render: (row) => statusBadge(row.status) },
            { key: 'channel', label: '渠道' },
            {
              key: 'is_core',
              label: '权重',
              className: 'num',
              render: (row) => (row.is_core ? '<span class="badge high">核心 ×3</span>' : '普通 ×1'),
            },
            { key: 'metric_count', label: '指标数', className: 'num' },
            { key: 'valid_from', label: '生效起', className: 'mono' },
            { key: 'valid_to', label: '生效止', className: 'mono', render: (row) => esc(row.valid_to || '至今') },
          ],
          empty: { title: '没有匹配的报表登记', hint: '调整状态或时点筛选。' },
        })}
      </section>
      <section class="panel" id="lineage-panel"></section>
      ${writable ? '<section class="panel" id="batch-panel"></section>' : ''}`;

    const filters = view.querySelector('#report-filters');
    filters?.addEventListener('change', () => {
      state.status = filters.querySelector('[name=status]').value;
      state.asOf = filters.querySelector('[name=as_of]').value;
      state.includeRetired = state.status === 'retired';
      reportsPage.render(view, ctx);
    });
    view.querySelector('#open-import')?.addEventListener('click', () => openImport(view, ctx));
    view.querySelector('#export-reports')?.addEventListener('click', async () => {
      try {
        const suffix = state.asOf ? `?as_of=${encodeURIComponent(state.asOf)}` : '';
        await api.download(`/api/exports/reports.csv${suffix}`, 'reports.csv');
        toast('报表登记 CSV 已下载');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const code = row.querySelector('.index-tag')?.textContent;
        const item = data.items.find((entry) => entry.code === code);
        if (item) openReport(view, ctx, item);
      });
    });
    await renderLineage(view);
    if (writable) await renderBatches(view);
  },
};

export default reportsPage;
