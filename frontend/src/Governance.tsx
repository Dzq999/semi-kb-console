import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, Sparkles, RefreshCw, Recycle, Clock } from "lucide-react";
import { api } from "./api";
import { useAppStore } from "./store";

// ---- 类型 ----
type Candidate = {
  id: number;
  kind: "class" | "property";
  canonical: string;
  canonical_module: string | null;
  merge: string[];
  reason: string[];
  label_zh: string | null;
  status: "detected" | "deprecated" | "merged" | "resolved";
  detected_at: string | null;
  deprecated_at: string | null;
  grace_until: string | null;
  grace_days_left: number | null;
  merged_at: string | null;
  merge_ready: boolean;
};

type Kpis = {
  candidate_counts: Record<string, number>;
  active_synonym_groups: number;
  merge_ready: number;
  link_specificity: number | null;
  referential_integrity: number | null;
  headline_coverage: number | null;
  last_scan_at: string | null;
  last_merge_at: string | null;
};

type GovJob = {
  id: string;
  kind: "scan" | "merge";
  trigger: "manual" | "cron";
  status: string;
  progress: number;
  stage: string | null;
  error: string | null;
  audit_path: string | null;
  output: string | null;
  created_at: string | null;
  completed_at: string | null;
};

type Schedule = {
  enabled: boolean;
  cron: string;
  timezone: string;
  auto_deprecate: boolean;
  grace_days: number;
  next_run_at: string | null;
  last_run_at: string | null;
};

const STATUS_LABEL: Record<string, string> = {
  detected: "已检出",
  deprecated: "已标记(grace)",
  merged: "已合并",
  resolved: "已消解",
};
const REASON_LABEL: Record<string, string> = {
  uri_variant: "URI 变体",
  same_label_en: "英文名撞名",
  same_label_zh: "中文名撞名",
};

function fmt(ts: string | null): string {
  return ts ? new Date(ts).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function GovernancePage() {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((s) => s.setNotice);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const kpis = useQuery<Kpis>({ queryKey: ["gov-kpis"], queryFn: () => api("/api/governance/kpis"), refetchInterval: 15000 });
  const candidates = useQuery<{ items: Candidate[] }>({ queryKey: ["gov-candidates"], queryFn: () => api("/api/governance/candidates") });
  const runs = useQuery<{ items: GovJob[] }>({
    queryKey: ["gov-runs"],
    queryFn: () => api("/api/governance/runs"),
    // 有作业在跑时快轮询，空闲停。
    refetchInterval: (q) => (q.state.data?.items?.some((j) => j.status === "queued" || j.status === "running") ? 2000 : false),
  });

  const running = runs.data?.items?.some((j) => j.status === "queued" || j.status === "running") ?? false;

  const refreshAll = () => {
    queryClient.invalidateQueries({ queryKey: ["gov-kpis"] });
    queryClient.invalidateQueries({ queryKey: ["gov-candidates"] });
    queryClient.invalidateQueries({ queryKey: ["gov-runs"] });
  };

  const scan = useMutation({
    mutationFn: () => api("/api/governance/runs", { method: "POST", body: JSON.stringify({ kind: "scan" }) }),
    onSuccess: () => { setNotice("同义扫描已启动，完成后自动刷新候选"); refreshAll(); },
  });
  const merge = useMutation({
    mutationFn: (ids: number[]) => api("/api/governance/runs", { method: "POST", body: JSON.stringify({ kind: "merge", candidate_ids: ids }) }),
    onSuccess: () => { setNotice("合并事务已启动（含发布门禁，约 13 分钟）；失败会自动回滚"); setSelected(new Set()); refreshAll(); },
  });
  const deprecate = useMutation({
    mutationFn: (ids: number[]) => api("/api/governance/candidates/deprecate", { method: "POST", body: JSON.stringify({ candidate_ids: ids, grace_days: 14 }) }),
    onSuccess: () => { setNotice("已标记为 deprecated 并起 grace 倒计时"); setSelected(new Set()); refreshAll(); },
  });

  const items = candidates.data?.items ?? [];
  const actionable = useMemo(() => items.filter((c) => c.status === "detected" || c.status === "deprecated"), [items]);
  const selectedRows = useMemo(() => items.filter((c) => selected.has(c.id)), [items, selected]);
  const canMerge = selectedRows.length > 0 && selectedRows.every((c) => c.status === "detected" || c.status === "deprecated");
  const canDeprecate = selectedRows.length > 0 && selectedRows.every((c) => c.status === "detected");

  const toggle = (id: number) => setSelected((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const toggleAll = () => setSelected((prev) => (prev.size === actionable.length ? new Set() : new Set(actionable.map((c) => c.id))));

  const k = kpis.data;
  const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>本体治理</h2>
          <p className="page-sub">同义重复修剪生命周期（检出 → 标记 grace → 人工确认合并）与治理 KPI。删除不可逆，永远由人确认。</p>
        </div>
        <button className="button button-primary" onClick={() => scan.mutate()} disabled={scan.isPending || running}>
          <Sparkles size={15} /> {running ? "作业进行中…" : "立即扫描同义"}
        </button>
      </div>

      {/* KPI 卡 */}
      <section className="panel">
        <header className="panel-head"><h3>治理 KPI</h3><div className="panel-meta">复用引擎既有质量报告 + 候选生命周期计数</div></header>
        <div className="metrics-grid">
          <div className="metric-card"><span>活跃同义组</span><strong>{k?.active_synonym_groups ?? "—"}</strong><small>待处理（检出+标记）</small></div>
          <div className="metric-card"><span>可合并</span><strong>{k?.merge_ready ?? "—"}</strong><small>已过 grace 期</small></div>
          <div className="metric-card"><span>已合并累计</span><strong>{k?.candidate_counts?.merged ?? "—"}</strong><small>历史落盘</small></div>
          <div className="metric-card"><span>链接特异性</span><strong>{pct(k?.link_specificity)}</strong><small>vFab×本体 headline</small></div>
          <div className="metric-card"><span>引用完整性</span><strong>{pct(k?.referential_integrity)}</strong><small>门禁硬线 100%</small></div>
        </div>
        <p className="hint">最近扫描：{fmt(k?.last_scan_at ?? null)}　·　最近合并：{fmt(k?.last_merge_at ?? null)}</p>
      </section>

      {/* 候选审阅表 */}
      <section className="panel">
        <header className="panel-head">
          <h3>同义候选</h3>
          <div className="panel-meta">
            {candidates.isFetching ? "刷新中…" : `${items.length} 组`}
          </div>
        </header>
        <div className="gov-actions">
          <button className="button" onClick={() => deprecate.mutate([...selected])} disabled={!canDeprecate || deprecate.isPending}>
            <Clock size={14} /> 标记 deprecated（起 grace）
          </button>
          <button className="button button-danger" onClick={() => merge.mutate([...selected])} disabled={!canMerge || merge.isPending || running}>
            <Recycle size={14} /> 确认合并落盘（不可逆 · {selectedRows.length}）
          </button>
          <span className="hint">合并会跑发布门禁（约 13 分钟），失败自动回滚；期间禁止编排任务并发写。</span>
        </div>
        {candidates.isLoading ? (
          <div className="loading"><RefreshCw className="spin" size={18} /> 正在加载候选…</div>
        ) : items.length === 0 ? (
          <p className="empty">暂无同义候选。点『立即扫描同义』检出。</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th><input type="checkbox" checked={selected.size > 0 && selected.size === actionable.length} onChange={toggleAll} /></th>
                  <th>类型</th><th>规范术语（保留）</th><th>合并项（删除）</th><th>判据</th><th>状态</th><th>grace 剩余</th>
                </tr>
              </thead>
              <tbody>
                {items.map((c) => {
                  const locked = c.status === "merged" || c.status === "resolved";
                  return (
                    <tr key={c.id} className={locked ? "row-muted" : ""}>
                      <td><input type="checkbox" checked={selected.has(c.id)} disabled={locked} onChange={() => toggle(c.id)} /></td>
                      <td>{c.kind === "class" ? "类" : "属性"}</td>
                      <td><code>{c.canonical}</code>{c.label_zh ? <small className="muted"> · {c.label_zh}</small> : null}</td>
                      <td>{c.merge.map((m) => <code key={m} className="strike">{m}</code>)}</td>
                      <td>{c.reason.map((r) => REASON_LABEL[r] || r).join("、")}</td>
                      <td><span className={`status-tag st-${c.status}`}>{STATUS_LABEL[c.status] || c.status}</span></td>
                      <td>{c.status === "deprecated" && c.grace_days_left !== null ? (c.grace_days_left <= 0 ? "可合并" : `${c.grace_days_left} 天`) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {(merge.error || deprecate.error || scan.error) ? (
          <div className="error-box">{String((merge.error || deprecate.error || scan.error) as Error)}</div>
        ) : null}
      </section>

      <ScheduleForm />

      {/* 作业历史 */}
      <section className="panel">
        <header className="panel-head"><h3>作业历史</h3><div className="panel-meta">扫描 / 合并 · 手动与定时</div></header>
        {runs.isLoading ? (
          <div className="loading"><RefreshCw className="spin" size={18} /> 加载中…</div>
        ) : (runs.data?.items?.length ?? 0) === 0 ? (
          <p className="empty">暂无作业记录。</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>类型</th><th>触发</th><th>状态</th><th>阶段/进度</th><th>创建</th><th>完成</th></tr></thead>
              <tbody>
                {runs.data!.items.map((j) => (
                  <tr key={j.id}>
                    <td>{j.kind === "scan" ? "扫描" : "合并"}</td>
                    <td>{j.trigger === "cron" ? "定时" : "手动"}</td>
                    <td><span className={`status-tag st-${j.status}`}>{j.status}</span>{j.error ? <small className="err"> · {j.error.slice(0, 80)}</small> : null}</td>
                    <td>{j.status === "running" ? `${j.stage || ""} ${Math.round(j.progress)}%` : j.stage || "—"}</td>
                    <td>{fmt(j.created_at)}</td>
                    <td>{fmt(j.completed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function ScheduleForm() {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((s) => s.setNotice);
  const schedule = useQuery<Schedule>({ queryKey: ["gov-schedule"], queryFn: () => api("/api/governance/schedule") });
  const [state, setState] = useState<Schedule | null>(null);
  useEffect(() => { if (schedule.data && !state) setState(schedule.data); }, [schedule.data, state]);

  const save = useMutation({
    mutationFn: () => api("/api/governance/schedule", {
      method: "PUT",
      body: JSON.stringify({
        enabled: state?.enabled ?? false,
        cron: state?.cron ?? "0 3 * * *",
        auto_deprecate: state?.auto_deprecate ?? true,
        grace_days: state?.grace_days ?? 14,
      }),
    }),
    onSuccess: () => { setNotice("定时治理设置已保存"); queryClient.invalidateQueries({ queryKey: ["gov-schedule"] }); },
  });

  if (!state) return <section className="panel"><header className="panel-head"><h3>定时治理（Cron）</h3></header><div className="loading"><RefreshCw className="spin" size={18} /> 加载中…</div></section>;

  return (
    <section className="panel">
      <header className="panel-head">
        <h3><ShieldCheck size={15} /> 定时治理（Cron）</h3>
        <div className="panel-meta">定时只做扫描（+可选自动标记 grace）；合并落盘永远由人确认，定时绝不触发</div>
      </header>
      <div className="settings-form">
        <label className="switch-line">
          <span>启用定时扫描</span>
          <input type="checkbox" checked={state.enabled} onChange={(e) => setState({ ...state, enabled: e.target.checked })} />
        </label>
        <label>
          Cron 表达式（分 时 日 月 周）
          <input type="text" value={state.cron} placeholder="0 3 * * *" onChange={(e) => setState({ ...state, cron: e.target.value })} />
          <small className="muted">如 <code>0 3 * * *</code>=每日 03:00；<code>0 */6 * * *</code>=每 6 小时。时区 {state.timezone}。</small>
        </label>
        <label className="switch-line">
          <span>扫描后自动标记 deprecated（起 grace 倒计时）</span>
          <input type="checkbox" checked={state.auto_deprecate} onChange={(e) => setState({ ...state, auto_deprecate: e.target.checked })} />
        </label>
        <label>
          grace 期（天）
          <input type="number" min={0} max={365} value={state.grace_days} onChange={(e) => setState({ ...state, grace_days: Number(e.target.value) })} />
        </label>
        <div className="form-actions">
          <button className="button button-primary" onClick={() => save.mutate()} disabled={save.isPending}>保存设置</button>
          <span className="hint">下次触发：{fmt(state.next_run_at)}　·　上次：{fmt(state.last_run_at)}</span>
        </div>
        {save.error ? <div className="error-box">{String(save.error as Error)}</div> : null}
      </div>
    </section>
  );
}

export default function Governance() {
  return <GovernancePage />;
}
