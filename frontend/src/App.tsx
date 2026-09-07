import { useEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  NavLink,
  Navigate,
  Route,
  Routes,
  useNavigate,
} from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  Archive,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleStop,
  Cpu,
  Database,
  Download,
  ExternalLink,
  FileText,
  Gauge,
  History,
  LayoutDashboard,
  LockKeyhole,
  LoaderCircle,
  Mail,
  MessagesSquare,
  Network,
  Newspaper,
  Pause,
  PauseCircle,
  Play,
  RefreshCw,
  Save,
  Search,
  Send,
  Settings,
  Share2,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
  Workflow,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  streamNdjson,
  type AgentConfig,
  type RunInfo,
  type RunReference,
  type SourceMode,
} from "./api";
import { useAppStore } from "./store";
import OntologyGraph from "./OntologyGraph";

type User = {
  id: number;
  username: string;
  preferences: {
    default_model_id?: string;
    default_agent_count: number;
    timezone: string;
    llm_base_url?: string | null;
    model_catalog_url?: string | null;
    llm_api_style?: string | null;
  };
};
type DashboardData = {
  metrics: {
    totals: Record<string, number | string>;
    today_added: Record<string, number>;
    source_distribution: Record<string, number>;
    uncovered: Array<{
      id: string;
      name_zh: string;
      severity: string;
      domain: string;
    }>;
  };
  latest_run: RunInfo | null;
};

type MetricKey =
  | "classes"
  | "properties"
  | "relations"
  | "individuals"
  | "axioms"
  | "rules"
  | "knowledge_entries"
  | "business_relations"
  | "simulation_scenarios"
  | "scenario_articles"
  | "segment_fab"
  | "segment_ap"
  | "segment_cross";

const metricDefinitions: Array<{ key: MetricKey; label: string }> = [
  { key: "classes", label: "类 Class" },
  { key: "properties", label: "属性 Property" },
  { key: "relations", label: "关系 Relation" },
  { key: "individuals", label: "实例 Individual" },
  { key: "segment_fab", label: "实例 · 前段厂 (fab)" },
  { key: "segment_ap", label: "实例 · 后段厂 (ap)" },
  { key: "segment_cross", label: "实例 · 跨段通用" },
  { key: "axioms", label: "公理 Axiom" },
  { key: "rules", label: "推理规则 Rule" },
  { key: "knowledge_entries", label: "知识条目" },
  { key: "business_relations", label: "经营模型关系" },
  { key: "simulation_scenarios", label: "仿真场景" },
  { key: "scenario_articles", label: "场景知识产物" },
];

const nav = [
  ["/", "总览", LayoutDashboard],
  ["/orchestrator", "任务编排", Bot],
  ["/ontology", "本体中心", Network],
  ["/ontology-graph", "关系图谱", Share2],
  ["/knowledge", "知识库", Database],
  ["/imports", "素材导入", Upload],
  ["/business", "经营模型", Gauge],
  ["/simulation", "仿真引擎", Cpu],
  ["/scenarios", "业务场景", FileText],
  ["/qa", "智能问答", MessagesSquare],
  ["/reports", "日报中心", Newspaper],
  ["/exports", "导出中心", Archive],
  ["/history", "运行历史", History],
  ["/settings", "系统设置", Settings],
] as const;

function Button({
  children,
  primary,
  danger,
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  primary?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      className={`button ${primary ? "button-primary" : ""} ${danger ? "button-danger" : ""} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

function Panel({
  title,
  meta,
  children,
  className = "",
}: {
  title: string;
  meta?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <h3>{title}</h3>
        {meta && <div className="panel-meta">{meta}</div>}
      </header>
      {children}
    </section>
  );
}

function Loading({ text = "正在加载真实数据…" }: { text?: string }) {
  return (
    <div className="loading">
      <RefreshCw className="spin" size={18} />
      {text}
    </div>
  );
}

function ErrorBox({ error }: { error: unknown }) {
  return (
    <div className="error-box">
      {error instanceof Error ? error.message : String(error)}
    </div>
  );
}

function AuthScreen({
  setupRequired,
  onAuthenticated,
}: {
  setupRequired: boolean;
  onAuthenticated: (user: User) => void;
}) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(setupRequired ? "/api/auth/setup" : "/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      onAuthenticated(await api<User>("/api/users/me"));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth-page">
      <form className="auth-box" onSubmit={submit}>
        <div className="auth-mark">
          <Workflow />
        </div>
        <h1>SEMI KB</h1>
        <p>
          {setupRequired ? "首次初始化管理员账户" : "登录半导体本体智能控制台"}
        </p>
        <label>
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            minLength={3}
            required
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={10}
            required
          />
        </label>
        {error && <ErrorBox error={error} />}
        <Button primary disabled={busy}>
          {busy ? "处理中…" : setupRequired ? "初始化并登录" : "登录"}
        </Button>
      </form>
    </div>
  );
}

function Layout({
  user,
  onLogout,
  onUserChange,
}: {
  user: User;
  onLogout: () => void;
  onUserChange: (user: User) => void;
}) {
  const notice = useAppStore((state) => state.notice);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Workflow size={18} />
          </span>
          <span>
            <strong>SEMI KB</strong>
            <small>Ontology Studio</small>
          </span>
        </div>
        <div className="nav-label">工作台</div>
        <nav>
          {nav.map(([path, label, Icon]) => (
            <NavLink key={path} to={path} end={path === "/"}>
              <Icon size={16} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="online-dot" />
          服务在线<small>{user.username}</small>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div>
            <h1>半导体本体智能控制台</h1>
            <p>Fab · FAC · EQP 领域知识持续演进</p>
          </div>
          <div className="top-actions">
            <span className="connection">
              <CheckCircle2 size={15} />
              semi-kb 已连接
            </span>
            <Button onClick={onLogout}>退出</Button>
          </div>
        </header>
        {notice && (
          <div className="global-notice" role="status" aria-live="polite">
            <CheckCircle2 size={15} aria-hidden="true" />
            <span>{notice}</span>
          </div>
        )}
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route
            path="/orchestrator"
            element={<Orchestrator user={user} onUserChange={onUserChange} />}
          />
          <Route path="/ontology" element={<Ontology />} />
          <Route path="/ontology-graph" element={<OntologyGraph />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="/imports" element={<ImportsPage />} />
          <Route path="/business" element={<Business />} />
          <Route path="/simulation" element={<Simulation />} />
          <Route path="/scenarios" element={<Scenarios />} />
          <Route path="/qa" element={<QaPage user={user} />} />
          <Route path="/reports" element={<Reports user={user} />} />
          <Route path="/exports" element={<ExportCenter />} />
          <Route path="/history" element={<RunHistory />} />
          <Route
            path="/settings"
            element={<SettingsPage user={user} onUserChange={onUserChange} />}
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function MetricCard({
  label,
  value,
  delta,
  note,
}: {
  label: string;
  value: number | string;
  delta?: number;
  note?: string;
}) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong>
        {typeof value === "number" ? value.toLocaleString() : value}
      </strong>
      <small>
        {delta !== undefined
          ? `今日新增 +${delta.toLocaleString()}`
          : "质量指标 · 百分比"}
      </small>
      {note ? <small className="metric-note">{note}</small> : null}
    </div>
  );
}

// individuals 卡副标注：基于12个策展模块的实例统计（与工艺段拆分使用相同基数）。
// 知识=agent 生成（model_prior/web/assumption），产线数据=真实产线/导入（当前为 0，即尚未接入实测）。
function individualsSplitNote(totals: Record<string, number | string>): string | undefined {
  const domain = Number(totals["individuals_domain"] ?? 0);
  if (!domain) return undefined;
  const knowledge = Number(totals["individuals_knowledge"] ?? 0);
  const operational = Number(totals["individuals_operational"] ?? 0);
  const untagged = Number(totals["individuals_untagged"] ?? 0);
  const parts = [`知识 ${knowledge.toLocaleString()}`, `产线数据 ${operational.toLocaleString()}`];
  if (untagged > 0) parts.push(`未标注 ${untagged.toLocaleString()}`);
  return `策展模块实例 ${domain.toLocaleString()}：${parts.join(" / ")}`;
}

function MetricsOverview({
  metrics,
  keys = metricDefinitions.map(({ key }) => key),
  className = "",
}: {
  metrics: DashboardData["metrics"];
  keys?: MetricKey[];
  className?: string;
}) {
  return (
    <div className={`metrics-grid ${className}`.trim()}>
      {keys.map((key) => {
        const definition = metricDefinitions.find((item) => item.key === key)!;
        // 只有 individuals 卡片显示来源拆分注释（知识/产线数据），工艺段卡片不显示
        const note = key === "individuals" ? individualsSplitNote(metrics.totals) : undefined;
        return (
          <MetricCard
            key={key}
            label={definition.label}
            value={metrics.totals[key] ?? 0}
            delta={metrics.today_added[key] ?? 0}
            note={note}
          />
        );
      })}
    </div>
  );
}

const stageLabels: Record<string, string> = {
  gap_analysis: "缺口分析",
  parallel_research: "并行研究",
  evidence_extraction: "证据提取",
  semantic_modeling: "语义建模",
  cross_validation: "交叉验证",
  candidate_repair: "候选修复",
  partial_publish: "部分发布",
  owl_shacl_reasoning: "OWL / SHACL",
  business_simulation: "经营仿真",
  scenario_article: "场景沉淀",
  finalize_round: "轮次归档",
  between_rounds: "轮次间隔",
  recovering: "断点恢复",
  round_failed: "轮次失败",
  completed: "完成",
  completed_partial: "部分完成",
  completed_no_change: "完成（无新增）",
};

function RunStatusIcon({ status }: { status?: string }) {
  if (status === "completed" || status === "completed_partial" || status === "completed_no_change") return <CheckCircle2 size={19} />;
  if (status === "failed" || status === "round_failed")
    return <AlertTriangle size={19} />;
  if (status === "paused") return <PauseCircle size={19} />;
  if (status === "needs_attention") return <AlertCircle size={19} />;
  if (status === "cancelling" || status === "cancelled") return <CircleStop size={19} />;
  if (status === "between_rounds") return <Pause size={18} />;
  return <LoaderCircle className="status-spin" size={19} />;
}

function ReferenceList({
  items,
  isLoading,
  total,
  compact = false,
  hasWebSource = true,
  hasRun = true,
}: {
  items?: RunReference[];
  isLoading?: boolean;
  total?: number;
  compact?: boolean;
  hasWebSource?: boolean;
  hasRun?: boolean;
}) {
  if (isLoading && !items?.length) return <Loading text="正在读取参考资料…" />;
  if (!items?.length)
    return (
      <div className="reference-empty">
        {!hasRun
          ? "启动任务后，网页参考资料会在此实时出现"
          : hasWebSource
            ? "本轮尚未产生可展示的网页参考资料"
            : "本轮 Agent 使用模型先验，没有网页参考资料"}
      </div>
    );
  return (
    <div className={`reference-list ${compact ? "compact" : ""}`}>
      <div className="reference-summary">
        <span>已收集 {total ?? items.length} 条去重来源</span>
        <small>来源链接来自实际检索结果</small>
      </div>
      {items.map((item) => {
        const host = (() => {
          try {
            return new URL(item.url).hostname;
          } catch {
            return item.url;
          }
        })();
        const status =
          item.fetch_status === "ok" ? "已抓取" : item.fetch_status;
        return (
          <article
            className="reference-item"
            key={`${item.url}-${item.provenance.map((entry) => `${entry.round}-${entry.agent_id}`).join(",")}`}
          >
            <div className="reference-main">
              <a href={item.url} target="_blank" rel="noreferrer noopener">
                {item.title}
                <ExternalLink size={12} aria-hidden="true" />
              </a>
              <div className="reference-meta">
                <span>{host}</span>
                <span>
                  第 {item.provenance[0]?.round ?? "-"} 轮 ·{" "}
                  {item.provenance[0]?.agent_name ?? "Agent"}
                </span>
                <span
                  className={`reference-fetch reference-fetch-${item.fetch_status}`}
                >
                  {status}
                </span>
              </div>
            </div>
            {item.excerpt && (
              <details className="reference-excerpt">
                <summary>查看摘要</summary>
                <p>{item.excerpt}</p>
              </details>
            )}
          </article>
        );
      })}
    </div>
  );
}

function Dashboard() {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const { data, error, isLoading } = useQuery<DashboardData>({
    queryKey: ["dashboard"],
    queryFn: () => api("/api/dashboard"),
    // 知识库累计指标慢变，10s 轮询足够；后端 metrics 缓存 20s，稳态下多数命中缓存。
    refetchInterval: 10000,
  });
  const [events, setEvents] = useState<
    Array<{ message: string; level: string; created_at: string }>
  >([]);
  const [resumeMaxFailures, setResumeMaxFailures] = useState(3);
  const latest = data?.latest_run;
  const canResume = Boolean(
    latest && ["paused", "needs_attention", "pending"].includes(latest.status),
  );
  useEffect(() => {
    if (latest?.max_consecutive_round_failures) {
      setResumeMaxFailures(latest.max_consecutive_round_failures);
    }
  }, [latest?.id, latest?.max_consecutive_round_failures]);
  const references = useQuery<{ items: RunReference[]; total: number }>({
    queryKey: ["run-references", latest?.id],
    queryFn: () => api(`/api/runs/${latest?.id}/references`),
    enabled: Boolean(latest?.id),
    refetchInterval:
      latest &&
      [
        "pending",
        "running",
        "recovering",
        "paused",
        "between_rounds",
        "stopping_after_round",
        "cancelling",
      ].includes(latest.status)
        ? 2500
        : false,
  });
  useEffect(() => {
    if (
      !latest?.id ||
      ![
        "pending",
        "running",
        "recovering",
        "paused",
        "between_rounds",
        "stopping_after_round",
        "cancelling",
      ].includes(latest.status)
    )
      return;
    const stream = new EventSource(`/api/runs/${latest.id}/events`);
    const handler = (event: MessageEvent) => {
      const item = JSON.parse(event.data);
      setEvents((items) => [item, ...items].slice(0, 30));
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({
        queryKey: ["run-references", latest.id],
      });
    };
    [
      "run_started",
      "round_started",
      "round_resumed",
      "stage_started",
      "graph_node_started",
      "agent_started",
      "agent_retry",
      "agent_cache_hit",
      "model_response_reused",
      "agent_completed",
      "agent_failed",
      "evidence_ready",
      "candidates_ready",
      "cross_validation",
      "gate_failed",
      "candidate_quarantined",
      "articles_ready",
      "round_completed",
      "round_failed",
      "round_wait",
      "attention_required",
      "run_completed",
      "run_failed",
      "run_cancelled",
    ].forEach((name) =>
      stream.addEventListener(name, handler as EventListener),
    );
    return () => stream.close();
  }, [latest?.id, latest?.status, queryClient]);
  const action = useMutation({
    mutationFn: ({ name, maxFailures }: { name: string; maxFailures?: number }) =>
      api(`/api/runs/${latest?.id}/${name}`, {
        method: "POST",
        ...(name === "resume" && maxFailures
          ? { body: JSON.stringify({ max_consecutive_round_failures: maxFailures }) }
          : {}),
      }),
    onSuccess: (_result, variables) => {
      setNotice(variables.name === "pause" ? "任务已暂停" : variables.name === "resume" ? "任务已恢复" : variables.name === "cancel" ? "任务已停止" : "已提交轮次停止请求");
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      if (variables.name === "cancel") {
        // The backend first reports `cancelling` and then persists `cancelled`
        // from the task's CancelledError handler. Refetch immediately so the
        // banner never remains on a stale running snapshot after a stop click.
        void queryClient.refetchQueries({ queryKey: ["dashboard"] });
      }
    },
  });
  if (isLoading)
    return (
      <div className="page">
        <Loading />
      </div>
    );
  if (error || !data)
    return (
      <div className="page">
        <ErrorBox error={error || "无数据"} />
      </div>
    );
  const t = data.metrics.totals;
  const currentIndex = Object.keys(stageLabels).indexOf(
    latest?.current_stage || "",
  );
  return (
    <div className="page">
      <section className={`loop-banner status-${latest?.status || "idle"}`}>
        <div className="loop-state">
          <span className="pulse-icon">
            <RunStatusIcon status={latest?.status} />
          </span>
          <div>
            <strong>
              {latest
                    ? `${latest.status === "completed" ? "持续协作任务已完成" : latest.status === "completed_partial" ? "持续协作任务部分完成" : latest.status === "completed_no_change" ? "持续协作任务完成（无新增）" : latest.status === "failed" ? "持续协作任务失败" : latest.status === "needs_attention" ? "持续协作任务需处理" : latest.status === "paused" ? "持续协作任务已暂停" : latest.status === "cancelling" ? "持续协作任务正在停止" : latest.status === "cancelled" ? "持续协作任务已停止" : latest.status === "recovering" ? "持续协作任务恢复中" : latest.status === "stopping_after_round" ? "持续协作任务将在本轮后停止" : `持续协作任务${latest.status === "between_rounds" ? "等待下一轮" : "运行中"}`} · 第 ${latest.current_round || 1} 轮`
                : "持续协作任务空闲"}
            </strong>
            <span>
              {latest
                ? `${latest.id} · ${stageLabels[latest.current_stage] || latest.current_stage}${latest.stop_after_round ? ` · 将在第 ${latest.stop_after_round} 轮结束` : ""}`
                : "从任务编排启动后，10 个标准 Agent 将协作完成全流程"}
            </span>
            {latest && (
              <span className="runtime-meta">
                {latest.orchestrator_engine} · checkpoint{" "}
                {latest.checkpoint_backend} · 恢复 {latest.recovery_count || 0}{" "}
                次
              </span>
            )}
          </div>
        </div>
        <div className="actions">
          {latest &&
            [
              "running",
              "pending",
              "recovering",
              "paused",
              "between_rounds",
              "stopping_after_round",
              "needs_attention",
            ].includes(latest.status) && (
              <>
                <Button
                  disabled={action.isPending}
                  onClick={() => {
                    const resume = canResume;
                    action.mutate({
                      name: resume ? "resume" : "pause",
                      ...(resume ? { maxFailures: resumeMaxFailures } : {}),
                    });
                  }}
                >
                  {canResume ? (
                    <Play size={15} />
                  ) : (
                    <Pause size={15} />
                  )}
                  {canResume
                    ? latest.status === "pending"
                      ? "启动任务"
                      : "恢复"
                    : "暂停"}
                </Button>
                {["needs_attention", "pending", "paused"].includes(latest.status) && (
                  <label className="resume-control">
                    继续失败上限
                    <input
                      type="number"
                      min="1"
                      max="20"
                      value={resumeMaxFailures}
                      onChange={(event) =>
                        setResumeMaxFailures(
                          Math.min(20, Math.max(1, Number(event.target.value))),
                        )
                      }
                      aria-label="恢复后的连续失败停止阈值"
                    />
                  </label>
                )}
                {latest.status !== "needs_attention" &&
                  !latest.stop_after_round && (
                    <Button
                      disabled={action.isPending}
                      onClick={() =>
                        action.mutate({ name: "stop-after-current-round" })
                      }
                    >
                      本轮结束后停止
                    </Button>
                  )}
                {latest.status !== "needs_attention" &&
                  !latest.stop_after_round && (
                    <Button
                      disabled={action.isPending}
                      onClick={() =>
                        action.mutate({ name: "stop-after-next-round" })
                      }
                    >
                      再运行一轮后停止
                    </Button>
                  )}
                <Button
                  danger
                  disabled={action.isPending}
                  onClick={() => action.mutate({ name: "cancel" })}
                >
                  <CircleStop size={15} />
                  立即停止
                </Button>
              </>
            )}
        </div>
      </section>
      {action.error && <ErrorBox error={action.error} />}
      <MetricsOverview metrics={data.metrics} />
      <div className="metric-support-row">
        <MetricCard
          label="问题域覆盖率"
          value={`${t.coverage_percent ?? 0}%`}
        />
        <span>覆盖率是质量指标，不适用“今日新增”数量口径。</span>
      </div>
      <div className="dashboard-grid">
        <Panel
          title="完整执行链路"
          meta={
            latest
              ? `已完成 ${latest.rounds_completed} 轮 · ${latest.publish_changes ? "门禁后自动发布" : "仅生成候选"}`
              : "尚无运行"
          }
        >
          <div className="pipeline">
            {Object.entries(stageLabels)
              .slice(0, 8)
              .map(([key, label], index) => (
                <div
                  key={key}
                  className={`stage ${latest?.current_stage === key ? "active" : ""} ${currentIndex > index || latest?.status === "completed" ? "done" : ""}`}
                >
                  <span>
                    {currentIndex > index || latest?.status === "completed" || latest?.status === "completed_partial" || latest?.status === "completed_no_change"
                      ? "✓"
                      : index + 1}
                  </span>
                  <small>{label}</small>
                </div>
              ))}
          </div>
          <div className="progress">
            <i style={{ width: `${latest?.progress || 0}%` }} />
          </div>
          <div className="progress-copy">
            <span>
              第 {latest?.current_round || 0} 轮 ·{" "}
              {stageLabels[latest?.current_stage || ""] || "等待任务"}
            </span>
            <strong>{latest?.progress || 0}%</strong>
          </div>
        </Panel>
        <Panel title="实时日志" meta="LIVE">
          <div className="logs">
            {events.length ? (
              events.map((event, index) => (
                <div
                  className={`log ${event.level}`}
                  key={`${event.created_at}-${index}`}
                >
                  <time>{new Date(event.created_at).toLocaleTimeString()}</time>
                  <span />
                  {event.message}
                </div>
              ))
            ) : (
              <div className="empty">运行后将在此显示实时事件</div>
            )}
          </div>
        </Panel>
      </div>
      <Panel
        title="执行参考资料"
        meta={references.data ? `${references.data.total} 条` : "实时更新"}
      >
        <ReferenceList
          items={references.data?.items}
          total={references.data?.total}
          isLoading={references.isLoading}
          hasRun={Boolean(latest)}
          hasWebSource={
            latest
              ? Boolean(
                  latest.agents.some(
                    (agent) =>
                      agent.source_mode === "web" ||
                      agent.source_mode === "hybrid",
                  ),
                )
              : true
          }
        />
      </Panel>
      <Panel title="质量与交叉验证" meta="真实基线">
        <div className="validation-row">
          <span>
            <ShieldCheck size={16} />
            内部特征已接入
          </span>
          <span className="warning">
            vFab：{String(t.vfab_state || "awaiting_source")}
          </span>
          <span>经营模型 {t.business_models || 0}</span>
          <span>仿真场景 {t.simulation_scenarios || 0}</span>
          <span>未覆盖问题 {data.metrics.uncovered.length}</span>
        </div>
      </Panel>
    </div>
  );
}

const agentTemplates = [
  ["Fab 研究", "工厂层级、产能、周期与WIP", "fab", "web"],
  ["FAC 研究", "厂务系统、能源与约束", "fac", "hybrid"],
  ["EQP 研究", "设备能力、状态与故障", "eqp", "web"],
  ["语义建模", "类、属性、关系、公理与规则", "core", "model_prior"],
  ["质量校验", "内部特征、OWL、SHACL与推理", "validation", "model_prior"],
  ["场景沉淀", "客户痛点、经营影响与文章", "scenario", "hybrid"],
  ["扩展研究", "补充高价值问题域", "fab", "web"],
  ["扩展研究", "补充高价值问题域", "fac", "web"],
  ["扩展研究", "补充高价值问题域", "eqp", "hybrid"],
  ["质量审查", "审查证据与发布门禁", "validation", "model_prior"],
] as const;

function Orchestrator({
  user,
  onUserChange,
}: {
  user: User;
  onUserChange: (user: User) => void;
}) {
  const navigate = useNavigate();
  const setLatestRun = useAppStore((state) => state.setLatestRun);
  const setNotice = useAppStore((state) => state.setNotice);
  const count = 10;
  const [modelSearch, setModelSearch] = useState(
    user.preferences.default_model_id || "",
  );
  const [selectedModel, setSelectedModel] = useState(
    user.preferences.default_model_id || "",
  );
  const [publishChanges, setPublishChanges] = useState(true);
  const [continuous, setContinuous] = useState(true);
  const [roundInterval, setRoundInterval] = useState(5);
  const [maxRounds, setMaxRounds] = useState(0);
  const [maxConsecutiveFailures, setMaxConsecutiveFailures] = useState(3);
  const [autoRepair, setAutoRepair] = useState(true);
  // 修复次数与连续失败阈值解耦：每次修复都要重跑整条闸门链，成本以分钟计。
  // 默认修复 1 次，不成就走部分发布，避免单轮被重试拖死。
  const [repairFollowsFailures, setRepairFollowsFailures] = useState(false);
  const [maxRepairAttempts, setMaxRepairAttempts] = useState(1);
  const [autoStart, setAutoStart] = useState(false);
  const [autoStartInterval, setAutoStartInterval] = useState(1440);
  const [agents, setAgents] = useState<AgentConfig[]>(() =>
    agentTemplates.map(([role, objective, domain, source]) => ({
      name: role,
      role,
      domain,
      objective,
      source_mode: source as SourceMode,
      timeout_seconds: 300,
      max_retries: 2,
    })),
  );
  const models = useQuery<{
    items: Array<{ id: string }>;
    total: number;
    default_model_id?: string;
    fetched_at: string;
  }>({ queryKey: ["models"], queryFn: () => api("/api/models?refresh=true") });
  const loop = useQuery<{
    enabled: boolean;
    interval_minutes: number;
    run_config?: { max_consecutive_round_failures?: number; max_auto_repair_attempts?: number; auto_repair?: boolean; repair_follow_failure_threshold?: boolean; max_rounds?: number | null };
  }>({
    queryKey: ["loop"],
    queryFn: () => api("/api/loop"),
  });
  useEffect(() => {
    if (!selectedModel && models.data?.default_model_id)
      setSelectedModel(models.data.default_model_id);
  }, [models.data?.default_model_id, selectedModel]);
  useEffect(() => {
    if (loop.data) {
      setAutoStart(loop.data.enabled);
      setAutoStartInterval(loop.data.interval_minutes);
      const configuredFailures =
        loop.data.run_config?.max_consecutive_round_failures;
      if (configuredFailures) setMaxConsecutiveFailures(configuredFailures);
      const configuredRepairs = loop.data.run_config?.max_auto_repair_attempts;
      if (configuredRepairs !== undefined) setMaxRepairAttempts(configuredRepairs);
      if (loop.data.run_config?.auto_repair !== undefined) setAutoRepair(loop.data.run_config.auto_repair);
      if (loop.data.run_config?.repair_follow_failure_threshold !== undefined) setRepairFollowsFailures(loop.data.run_config.repair_follow_failure_threshold);
      const configuredRounds = loop.data.run_config?.max_rounds;
      setMaxRounds(configuredRounds ?? 0);
    }
  }, [loop.data]);
  const filtered = useMemo(
    () =>
      (models.data?.items || []).filter((item) =>
        item.id.toLowerCase().includes(modelSearch.toLowerCase()),
      ),
    [models.data?.items, modelSearch],
  );
  const config = () => ({
    model_id: selectedModel,
    agents: agents.slice(0, count),
    publish_changes: publishChanges,
    continuous,
    round_interval_seconds: roundInterval,
    max_rounds: maxRounds > 0 ? maxRounds : null,
    max_consecutive_round_failures: maxConsecutiveFailures,
    auto_repair: autoRepair,
    max_auto_repair_attempts: repairFollowsFailures ? maxConsecutiveFailures : maxRepairAttempts,
    repair_follow_failure_threshold: repairFollowsFailures,
  });
  const start = useMutation({
    mutationFn: () =>
      api<RunInfo>("/api/runs", {
        method: "POST",
        body: JSON.stringify(config()),
      }),
    onSuccess: (run) => {
      setLatestRun(run);
      setNotice(`持续任务 ${run.id} 已启动`);
      navigate("/");
    },
  });
  const saveLoop = useMutation({
    mutationFn: () =>
      api("/api/loop", {
        method: "PUT",
        body: JSON.stringify({
          enabled: autoStart,
          interval_minutes: autoStartInterval,
          run_config: config(),
        }),
      }),
    onSuccess: () => {
      setNotice(autoStart ? "自动启动配置已保存" : "自动启动已关闭");
      loop.refetch();
    },
  });
  const saveDefault = useMutation({
    mutationFn: () =>
      api("/api/users/me/preferences/default-model", {
        method: "PATCH",
        body: JSON.stringify({ model_id: selectedModel }),
      }),
    onSuccess: () => {
      onUserChange({
        ...user,
        preferences: { ...user.preferences, default_model_id: selectedModel },
      });
      setNotice(`默认模型已设为 ${selectedModel}`);
    },
  });
  const updateSource = (index: number, source_mode: SourceMode) =>
    setAgents((items) =>
      items.map((item, itemIndex) =>
        itemIndex === index ? { ...item, source_mode } : item,
      ),
    );
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>任务编排</h2>
          <p>
            同一组标准 Agent 按轮协作执行完整流程，直到用户要求在轮次边界停止。
          </p>
        </div>
        <Button
          primary
          disabled={!selectedModel || start.isPending}
          onClick={() => start.mutate()}
        >
          <Play size={15} />
          {start.isPending ? "启动中…" : "启动标准 Agent 协作"}
        </Button>
      </div>
      {(models.error ||
        loop.error ||
        start.error ||
        saveLoop.error ||
        saveDefault.error) && (
        <ErrorBox
          error={
            models.error ||
            loop.error ||
            start.error ||
            saveLoop.error ||
            saveDefault.error
          }
        />
      )}
      <div className="compose-grid">
        <Panel
          title="模型与并发"
          meta={models.data ? `已同步 ${models.data.total} 个模型` : "等待同步"}
        >
          <div className="config-grid">
            <label>
              搜索并选择模型
              <div className="search-input">
                <Search size={15} />
                <input
                  value={modelSearch}
                  onChange={(e) => setModelSearch(e.target.value)}
                  placeholder="输入模型名称或ID"
                />
              </div>
              <select
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
              >
                <option value="">请选择模型</option>
                {filtered.map((item) => (
                  <option key={item.id}>{item.id}</option>
                ))}
              </select>
              <small className="model-count">
                显示 {filtered.length} / {models.data?.total || 0} 个模型
              </small>
            </label>
            <div className="agent-count">
              <span>标准 Agent 编制</span>
              <strong>固定 {count} 个</strong>
              <small>
                所有 Agent 协作完成一轮全流程；每个 Agent 可独立选择知识来源。
              </small>
            </div>
          </div>
          <div className="model-actions">
            <Button
              onClick={() => models.refetch().then(() => setNotice("模型列表已刷新"))}
              disabled={models.isFetching}
            >
              <RefreshCw size={14} />
              {models.isFetching ? "刷新中…" : "刷新模型"}
            </Button>
            <Button
              onClick={() => saveDefault.mutate()}
              disabled={!selectedModel || saveDefault.isPending}
            >
              <Save size={14} />
              {saveDefault.isPending ? "保存中…" : "设为默认模型"}
            </Button>
            <small>启动任务前后端会再次刷新并校验模型可用性</small>
          </div>
        </Panel>
        <Panel title="来源策略摘要" meta="逐个生效">
          <div className="source-summary">
            {(["web", "model_prior", "hybrid"] as SourceMode[]).map((mode) => (
              <div key={mode}>
                <strong>
                  {
                    agents
                      .slice(0, count)
                      .filter((item) => item.source_mode === mode).length
                  }
                </strong>
                <span>
                  {mode === "web"
                    ? "自行搜索资料"
                    : mode === "model_prior"
                      ? "预训练知识"
                      : "混合模式"}
                </span>
              </div>
            ))}
          </div>
          <p className="info-note">
            <ShieldCheck size={15} />
            联网结果保存正文、URL、时间与哈希；模型先验不会伪装成外部证据。
          </p>
        </Panel>
      </div>
      <Panel title="持续循环、发布与自动启动" meta="轮次级安全控制">
        <div className="loop-config">
          <label className="switch-line">
            <span>持续进入下一轮</span>
            <input
              type="checkbox"
              checked={continuous}
              onChange={(e) => setContinuous(e.target.checked)}
            />
          </label>
          <label className="switch-line">
            <span>全部门禁通过后自动发布本体、知识库和仿真</span>
            <input
              type="checkbox"
              checked={publishChanges}
              onChange={(e) => setPublishChanges(e.target.checked)}
            />
          </label>
          <label>
            轮次间隔（秒）
            <input
              type="number"
              min="0"
              max="86400"
              value={roundInterval}
              onChange={(e) => setRoundInterval(Number(e.target.value))}
            />
          </label>
          <label>
            最大轮次（0=无限循环）
            <input
              type="number"
              min="0"
              max="1000"
              value={maxRounds}
              onChange={(e) =>
                setMaxRounds(Math.min(1000, Math.max(0, Number(e.target.value))))
              }
            />
            <small className="field-hint">
              执行到该轮次后自动停止；设为 0 则持续循环直到人工停止。
            </small>
          </label>
          <label>
            连续失败停止阈值（轮）
            <input
              type="number"
              min="1"
              max="20"
              value={maxConsecutiveFailures}
              onChange={(e) =>
                setMaxConsecutiveFailures(
                  Math.min(20, Math.max(1, Number(e.target.value))),
                )
              }
            />
            <small className="field-hint">
              连续门禁失败达到该轮数后暂停并等待人工处理。
            </small>
          </label>
          <label className="switch-line">
            <span>门禁失败后自动返修</span>
            <input type="checkbox" checked={autoRepair} onChange={(e) => setAutoRepair(e.target.checked)} />
          </label>
          <label className="switch-line">
            <span>返修次数跟随连续失败阈值</span>
            <input type="checkbox" checked={repairFollowsFailures} onChange={(e) => setRepairFollowsFailures(e.target.checked)} />
          </label>
          <label>
            单轮最大自动返修次数
            <input type="number" min="0" max="10" value={maxRepairAttempts} disabled={repairFollowsFailures} onChange={(e) => setMaxRepairAttempts(Math.min(10, Math.max(0, Number(e.target.value))))} />
            <small className="field-hint">仅对可修复的完整性问题调用原 Agent；安全与来源问题直接隔离。</small>
          </label>
          <label className="switch-line">
            <span>后端启动后按计划自动拉起持续任务</span>
            <input
              type="checkbox"
              checked={autoStart}
              onChange={(e) => setAutoStart(e.target.checked)}
            />
          </label>
          <label>
            自动拉起检查间隔（分钟）
            <input
              type="number"
              min="5"
              max="43200"
              value={autoStartInterval}
              onChange={(e) => setAutoStartInterval(Number(e.target.value))}
            />
          </label>
          <Button
            onClick={() => saveLoop.mutate()}
            disabled={!selectedModel || saveLoop.isPending}
          >
            <Save size={14} />
            {saveLoop.isPending ? "保存中…" : "保存自动启动配置"}
          </Button>
        </div>
        <div className="warning-box loop-warning">
          自动发布仅在 JSON
          Schema、来源、内部特征/vFab、能力问题、OWL、SHACL、推理、经营模型和仿真全部通过后执行；可修复门禁失败会按设置回传原 Agent，仍失败的候选隔离，合法候选可部分发布。
        </div>
      </Panel>
      <Panel title="Agent 分工与知识来源" meta="每行单独配置">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>子 Agent</th>
                <th>职责</th>
                <th>问题域</th>
                <th>知识来源</th>
                <th>模型</th>
              </tr>
            </thead>
            <tbody>
              {agents.slice(0, count).map((agent, index) => (
                <tr key={index}>
                  <td>
                    <span className="agent-index">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    {agent.name}
                  </td>
                  <td>
                    <input
                      value={agent.objective}
                      onChange={(e) =>
                        setAgents((items) =>
                          items.map((item, i) =>
                            i === index
                              ? { ...item, objective: e.target.value }
                              : item,
                          ),
                        )
                      }
                    />
                  </td>
                  <td>
                    <input
                      className="short-input"
                      value={agent.domain}
                      onChange={(e) =>
                        setAgents((items) =>
                          items.map((item, i) =>
                            i === index
                              ? { ...item, domain: e.target.value }
                              : item,
                          ),
                        )
                      }
                    />
                  </td>
                  <td>
                    <select
                      value={agent.source_mode}
                      onChange={(e) =>
                        updateSource(index, e.target.value as SourceMode)
                      }
                    >
                      <option value="web">自行搜索资料</option>
                      <option value="model_prior">预训练知识</option>
                      <option value="hybrid">混合模式</option>
                    </select>
                  </td>
                  <td>
                    <span className="inherit">
                      继承 {selectedModel || "任务模型"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function ExportButton({
  kind,
  label = "导出",
}: {
  kind: string;
  label?: string;
}) {
  const setNotice = useAppStore((state) => state.setNotice);
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () =>
      api<{ id: string }>("/api/exports", {
        method: "POST",
        body: JSON.stringify({ kind }),
      }),
    onSuccess: ({ id }) => {
      setNotice(`导出任务 ${id} 已创建，可在导出中心查看进度`);
      queryClient.invalidateQueries({ queryKey: ["exports"] });
    },
  });
  return (
    <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
      <Download size={15} />
      {mutation.isPending ? "创建导出任务…" : label}
    </Button>
  );
}

function ExportCenter() {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const query = useQuery<{
    items: Array<{
      id: string;
      kind: string;
      status: string;
      progress: number;
      total_files: number;
      processed_files: number;
      error?: string;
      created_at: string;
      completed_at?: string;
      download_url?: string;
    }>;
  }>({
    queryKey: ["exports"],
    queryFn: () => api("/api/exports"),
    refetchInterval: 2000,
  });
  const retry = useMutation({
    mutationFn: (id: string) =>
      api(`/api/exports/${id}/retry`, { method: "POST" }),
    onSuccess: () => { setNotice("导出任务已重新排队"); queryClient.invalidateQueries({ queryKey: ["exports"] }); },
  });
  const cancel = useMutation({
    mutationFn: (id: string) =>
      api(`/api/exports/${id}/cancel`, { method: "POST" }),
    onSuccess: () => { setNotice("导出任务已取消"); queryClient.invalidateQueries({ queryKey: ["exports"] }); },
  });
  const create = useMutation({
    mutationFn: (kind: string) =>
      api("/api/exports", { method: "POST", body: JSON.stringify({ kind }) }),
    onSuccess: () => { setNotice("导出任务已创建，可在列表查看进度"); queryClient.invalidateQueries({ queryKey: ["exports"] }); },
  });
  const kinds = [
    ["ontology", "本体"],
    ["knowledge", "知识库"],
    ["business", "经营模型"],
    ["simulation", "仿真引擎"],
    ["scenarios", "场景知识产物"],
    ["complete", "完整包"],
  ];
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>导出中心</h2>
          <p>
            本体、知识库、经营模型、仿真引擎、场景知识产物均可独立导出，也可导出完整包。
          </p>
        </div>
        <div className="actions">
          {kinds.map(([kind, label]) => (
            <Button
              key={kind}
              primary={kind === "complete"}
              onClick={() => create.mutate(kind)}
              disabled={create.isPending}
            >
              <Download size={14} />
              导出{label}
            </Button>
          ))}
        </div>
      </div>
      <Panel title="导出任务" meta={`${query.data?.items.length || 0} 条`}>
        {query.isLoading ? (
          <Loading />
        ) : query.error ? (
          <ErrorBox error={query.error} />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>进度</th>
                  <th>文件</th>
                  <th>创建时间</th>
                  <th>完成时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {query.data?.items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <code>{item.id}</code>
                    </td>
                    <td>{item.kind}</td>
                    <td>
                      <span className={`status-tag ${item.status}`}>
                        {item.status}
                      </span>
                      {item.error && (
                        <small className="error-text">{item.error}</small>
                      )}
                    </td>
                    <td>
                      <progress value={item.progress || 0} max={100} />{" "}
                      {Math.round(item.progress || 0)}%
                    </td>
                    <td>
                      {item.processed_files}/{item.total_files}
                    </td>
                    <td>{new Date(item.created_at).toLocaleString()}</td>
                    <td>
                      {item.completed_at
                        ? new Date(item.completed_at).toLocaleString()
                        : "-"}
                    </td>
                    <td>
                      {item.download_url && (
                        <a className="button" href={item.download_url}>
                          下载
                        </a>
                      )}
                      {["failed", "cancelled"].includes(item.status) && (
                        <Button onClick={() => retry.mutate(item.id)}>
                          重试
                        </Button>
                      )}
                      {["queued", "running"].includes(item.status) && (
                        <Button danger onClick={() => cancel.mutate(item.id)}>
                          取消
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

type OntoNode = {
  iri: string;
  label: string;
  parents: string[];
  module: string;
};

function OntologyTree() {
  const tree = useQuery<{ nodes: OntoNode[]; total: number }>({
    queryKey: ["ontology-tree"],
    queryFn: () => api("/api/ontology/tree"),
  });
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [treeSearch, setTreeSearch] = useState("");

  const model = useMemo(() => {
    const nodes = tree.data?.nodes ?? [];
    const byIri = new Map(nodes.map((n) => [n.iri, n]));
    const children = new Map<string, OntoNode[]>();
    const roots: OntoNode[] = [];
    for (const n of nodes) {
      const real = n.parents.filter((p) => byIri.has(p) && p !== n.iri);
      if (real.length === 0) roots.push(n);
      for (const p of real) {
        const arr = children.get(p) ?? [];
        arr.push(n);
        children.set(p, arr);
      }
    }
    const descCount = new Map<string, number>();
    const active = new Set<string>();
    const count = (iri: string): number => {
      const cached = descCount.get(iri);
      if (cached !== undefined) return cached;
      if (active.has(iri)) return 0; // 防御环
      active.add(iri);
      const kids = children.get(iri) ?? [];
      let total = kids.length;
      for (const k of kids) total += count(k.iri);
      active.delete(iri);
      descCount.set(iri, total);
      return total;
    };
    for (const n of nodes) count(n.iri);
    const sortFn = (a: OntoNode, b: OntoNode) =>
      (descCount.get(b.iri)! - descCount.get(a.iri)!) ||
      a.label.localeCompare(b.label, "zh");
    roots.sort(sortFn);
    for (const arr of children.values()) arr.sort(sortFn);
    return { byIri, children, roots, descCount };
  }, [tree.data]);

  // 首次载入时展开一层，让 Entity 下的主分类立即可见（用户后续折叠不再覆盖）
  const seeded = useRef(false);
  useEffect(() => {
    if (!seeded.current && model.roots.length > 0) {
      seeded.current = true;
      setExpanded(new Set(model.roots.map((r) => r.iri)));
    }
  }, [model.roots]);

  const searching = treeSearch.trim().length > 0;
  const matchInfo = useMemo(() => {
    if (!searching) return null;
    const q = treeSearch.trim().toLowerCase();
    const subtreeHit = new Set<string>();
    const selfHit = new Set<string>();
    const seen = new Set<string>();
    const visit = (iri: string): boolean => {
      if (seen.has(iri)) return subtreeHit.has(iri);
      seen.add(iri);
      const node = model.byIri.get(iri);
      const self =
        !!node &&
        (node.label.toLowerCase().includes(q) ||
          node.iri.toLowerCase().includes(q));
      let hit = self;
      for (const k of model.children.get(iri) ?? []) {
        if (visit(k.iri)) hit = true;
      }
      if (self) selfHit.add(iri);
      if (hit) subtreeHit.add(iri);
      return hit;
    };
    for (const r of model.roots) visit(r.iri);
    return { subtreeHit, selfHit };
  }, [searching, treeSearch, model]);

  const toggle = (iri: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(iri)) next.delete(iri);
      else next.add(iri);
      return next;
    });

  const renderRow = (node: OntoNode, depth: number): React.ReactNode => {
    const kids = model.children.get(node.iri) ?? [];
    const hasKids = kids.length > 0;
    const isExpanded = searching
      ? !!matchInfo && kids.some((k) => matchInfo.subtreeHit.has(k.iri))
      : expanded.has(node.iri);
    const visibleKids =
      searching && matchInfo
        ? kids.filter((k) => matchInfo.subtreeHit.has(k.iri))
        : kids;
    const dcount = model.descCount.get(node.iri) ?? 0;
    const isHit = searching && !!matchInfo?.selfHit.has(node.iri);
    return (
      <div key={node.iri} className="onto-branch">
        <div
          className="onto-row"
          style={{ paddingLeft: depth * 15 + 8 }}
        >
          {hasKids ? (
            <button
              type="button"
              className="onto-toggle"
              onClick={() => toggle(node.iri)}
              disabled={searching}
              aria-label={isExpanded ? "收起" : "展开"}
            >
              {isExpanded ? (
                <ChevronDown size={13} />
              ) : (
                <ChevronRight size={13} />
              )}
            </button>
          ) : (
            <span className="onto-leaf" />
          )}
          <span className={`onto-label ${isHit ? "is-hit" : ""}`}>
            {node.label}
          </span>
          {dcount > 0 && <span className="onto-count">{dcount}</span>}
          <span className="onto-mod">{node.module}</span>
        </div>
        {isExpanded &&
          visibleKids.map((k) => renderRow(k, depth + 1))}
      </div>
    );
  };

  const visibleRoots = model.roots.filter(
    (r) => !searching || !!matchInfo?.subtreeHit.has(r.iri),
  );

  return (
    <Panel
      title="类层级"
      meta={
        <span>
          {model.roots.length} 个根 · {tree.data?.total ?? 0} 个类
        </span>
      }
    >
      <div className="filter-bar onto-toolbar">
        <div className="search-input">
          <Search size={15} />
          <input
            value={treeSearch}
            onChange={(e) => setTreeSearch(e.target.value)}
            placeholder="搜索类名或IRI，自动展开命中路径"
          />
        </div>
        <div className="onto-toolbar-actions">
          <button
            type="button"
            className="button"
            disabled={searching}
            onClick={() => setExpanded(new Set(model.byIri.keys()))}
          >
            展开全部
          </button>
          <button
            type="button"
            className="button"
            disabled={searching}
            onClick={() => setExpanded(new Set())}
          >
            收起全部
          </button>
        </div>
      </div>
      {tree.isLoading ? (
        <Loading />
      ) : tree.error ? (
        <ErrorBox error={tree.error} />
      ) : visibleRoots.length === 0 ? (
        <div className="empty">未找到匹配的类。</div>
      ) : (
        <div className="onto-tree">
          {visibleRoots.map((r) => renderRow(r, 0))}
        </div>
      )}
    </Panel>
  );
}

function Ontology() {
  const metrics = useQuery<DashboardData["metrics"]>({
    queryKey: ["ontology-metrics"],
    queryFn: () => api("/api/ontology/metrics"),
  });
  const [search, setSearch] = useState("");
  const entities = useQuery<{
    items: Array<{ iri: string; label: string; kind: string }>;
    total: number;
  }>({
    queryKey: ["entities", search],
    queryFn: () =>
      api(
        `/api/ontology/entities?search=${encodeURIComponent(search)}&limit=200`,
      ),
  });
  if (metrics.isLoading)
    return (
      <div className="page">
        <Loading />
      </div>
    );
  if (metrics.error)
    return (
      <div className="page">
        <ErrorBox error={metrics.error} />
      </div>
    );
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>本体中心</h2>
          <p>标准OWL语义、SHACL约束和可执行推理规则。</p>
        </div>
        <ExportButton kind="ontology" />
      </div>
      <MetricsOverview metrics={metrics.data!} />
      <OntologyTree />
      <Panel title="本体实体" meta={`${entities.data?.total || 0} 项`}>
        <div className="filter-bar">
          <div className="search-input">
            <Search size={15} />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索标签或IRI"
            />
          </div>
        </div>
        {entities.isLoading ? (
          <Loading />
        ) : entities.error ? (
          <ErrorBox error={entities.error} />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>类型</th>
                  <th>名称</th>
                  <th>IRI</th>
                </tr>
              </thead>
              <tbody>
                {entities.data?.items.map((item) => (
                  <tr key={`${item.kind}-${item.iri}`}>
                    <td>
                      <span className="type-tag">{item.kind}</span>
                    </td>
                    <td>{item.label}</td>
                    <td>
                      <code>{item.iri}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

function Knowledge() {
  const query = useQuery<{
    items: Array<{ path: string; name: string; document: unknown }>;
  }>({ queryKey: ["knowledge"], queryFn: () => api("/api/knowledge/facts") });
  return (
    <CatalogPage
      title="知识库"
      subtitle="事实、实例、证据与来源可追溯。"
      icon={<Database />}
      exportKind="knowledge"
      metricKeys={["knowledge_entries"]}
      query={query}
    />
  );
}

type ImportDeclaredClass = { iri: string; label: string };
type ImportFileMapping = {
  stored_name: string;
  format: string;
  headers: string[];
  derived_from?: string | null;
  target_class: string;
  entity_keys: string[];
  time_field?: string | null;
  target_class_valid: boolean;
};
type ImportValidation = {
  passed?: boolean;
  status?: string;
  error?: string | null;
  datasets?: Array<{ path: string; target_class: string; field_count: number }>;
};
type ImportJobSummary = {
  job_id: string;
  status: string;
  channel: string;
  classification: string;
  summary: string;
  llm_model_id: string;
  manifest: {
    files?: ImportFileMapping[];
    staged_only?: Array<{ stored_name: string; note: string }>;
    declared_classes?: ImportDeclaredClass[];
  };
  validation: ImportValidation;
  adopted_paths: { files?: string[]; gate_output?: string };
  created_at: string;
  adopted_at?: string | null;
  rejected_at?: string | null;
  uploaded?: Array<{ original_name: string; stored_name: string; format: string; size_bytes: number }>;
};

const IMPORT_STATUS_LABEL: Record<string, string> = {
  uploaded: "已上传",
  analyzed: "已分析",
  validated: "校验通过",
  invalid: "校验未过",
  adopted: "已采纳",
  rejected: "已丢弃",
};

function ImportsPage() {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const models = useQuery<{ items: Array<{ id: string }>; default_model_id?: string }>({
    queryKey: ["models"],
    queryFn: () => api("/api/models?refresh=true"),
    retry: false,
  });
  const jobs = useQuery<{ items: ImportJobSummary[] }>({
    queryKey: ["imports"],
    queryFn: () => api("/api/imports"),
  });
  const [files, setFiles] = useState<File[]>([]);
  const [classification, setClassification] = useState("internal_confidential");
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: async () => {
      const form = new FormData();
      files.forEach((file) => form.append("files", file));
      form.append("classification", classification);
      const response = await fetch("/api/imports", { method: "POST", credentials: "include", body: form });
      if (!response.ok) {
        const body = await response.json().catch(() => ({ detail: response.statusText }));
        const detail = Array.isArray(body.detail)
          ? body.detail.map((item: { msg?: string }) => item.msg).join("；")
          : body.detail;
        throw new ApiError(response.status, detail || `HTTP ${response.status}`);
      }
      return response.json() as Promise<ImportJobSummary>;
    },
    onSuccess: () => {
      setNotice("素材已上传，可在下方任务里分析与采纳");
      setFiles([]);
      if (fileInputRef.current) fileInputRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["imports"] });
    },
  });
  const fileCount = files.length;
  const addFiles = (incoming: FileList | null) => {
    if (!incoming || incoming.length === 0) return;
    setFiles((current) => {
      const seen = new Set(current.map((file) => `${file.name}:${file.size}`));
      const merged = [...current];
      Array.from(incoming).forEach((file) => {
        const key = `${file.name}:${file.size}`;
        if (!seen.has(key)) {
          seen.add(key);
          merged.push(file);
        }
      });
      return merged;
    });
  };
  const removeFile = (index: number) =>
    setFiles((current) => current.filter((_, i) => i !== index));
  const formatSize = (bytes: number) =>
    bytes < 1024
      ? `${bytes} B`
      : bytes < 1024 * 1024
        ? `${(bytes / 1024).toFixed(1)} KB`
        : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  const classificationOptions: Array<{ value: string; label: string; hint: string }> = [
    { value: "internal", label: "internal（内部）", hint: "内部素材，可分析、校验后采纳入库。" },
    { value: "internal_confidential", label: "internal_confidential（内部机密）", hint: "机密素材，独立来源标注，可采纳入库。" },
    { value: "restricted", label: "restricted（受限）", hint: "受限素材仅暂存留痕，禁止采纳入库。" },
  ];
  const activeClassification = classificationOptions.find((item) => item.value === classification);
  return (
    <div className="page">
      <Panel title="素材导入" meta="上传 · 模型建议映射 · 引擎门禁校验 · 人工采纳">
        <p className="import-intro">
          上传结构化素材，模型在已声明本体类约束下建议目标类与主键映射，经引擎真实门禁校验后由你采纳入库，用于交叉验证与知识库补充。
        </p>
        <div className="import-tags">
          <span className="import-tag">xlsx · xls · csv · tsv · json · md</span>
          <span className="import-tag">sha256 原件锁定不可变</span>
          <span className="import-tag">绝不自动写入推理层</span>
        </div>
        <div className="import-form">
          <div
            className={`dropzone ${dragging ? "dragover" : ""} ${fileCount ? "has-files" : ""}`}
            role="button"
            tabIndex={0}
            onClick={() => fileInputRef.current?.click()}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                fileInputRef.current?.click();
              }
            }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              addFiles(event.dataTransfer.files);
            }}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".xlsx,.xls,.csv,.tsv,.json,.md,.txt"
              onChange={(event) => {
                addFiles(event.target.files);
                event.target.value = "";
              }}
            />
            <span className="dropzone-icon">
              <Upload size={20} />
            </span>
            <strong>拖拽文件到此处，或点击选择</strong>
            <small>支持批量上传 · 可多次添加，重复文件自动去重</small>
          </div>
          {fileCount > 0 && (
            <ul className="file-chips">
              {files.map((file, index) => (
                <li key={`${file.name}:${file.size}:${index}`} className="file-chip">
                  <FileText size={13} />
                  <span className="file-chip-name" title={file.name}>{file.name}</span>
                  <span className="file-chip-size">{formatSize(file.size)}</span>
                  <button
                    type="button"
                    className="file-chip-remove"
                    aria-label={`移除 ${file.name}`}
                    onClick={() => removeFile(index)}
                  >
                    <X size={13} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="import-actions">
            <label className="import-classification">
              <span>密级</span>
              <select value={classification} onChange={(event) => setClassification(event.target.value)}>
                {classificationOptions.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </label>
            <small className="import-classification-hint">{activeClassification?.hint}</small>
            <Button primary onClick={() => upload.mutate()} disabled={fileCount === 0 || upload.isPending}>
              <Upload size={15} />
              {upload.isPending ? "上传中…" : `上传${fileCount ? `（${fileCount}）` : ""}`}
            </Button>
          </div>
        </div>
        {upload.error && <ErrorBox error={upload.error} />}
      </Panel>
      {jobs.isLoading ? (
        <Loading />
      ) : jobs.error ? (
        <ErrorBox error={jobs.error} />
      ) : (jobs.data?.items || []).length === 0 ? (
        <Panel title="导入任务">
          <small>暂无导入任务。</small>
        </Panel>
      ) : (
        <div className="catalog-grid">
          {(jobs.data?.items || []).map((job) => (
            <ImportJobCard
              key={job.job_id}
              job={job}
              models={models.data?.items || []}
              defaultModel={models.data?.default_model_id}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ImportJobCard({
  job,
  models,
  defaultModel,
}: {
  job: ImportJobSummary;
  models: Array<{ id: string }>;
  defaultModel?: string;
}) {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const [model, setModel] = useState(job.llm_model_id || defaultModel || "");
  const [draft, setDraft] = useState<ImportFileMapping[]>(job.manifest.files || []);
  useEffect(() => {
    setDraft(job.manifest.files || []);
  }, [job.manifest.files]);
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["imports"] });
  const classes = job.manifest.declared_classes || [];
  const restricted = job.classification === "restricted";

  const analyze = useMutation({
    mutationFn: () =>
      api<ImportJobSummary>(`/api/imports/${job.job_id}/analyze`, {
        method: "POST",
        body: JSON.stringify({ model_id: model || defaultModel || "" }),
      }),
    onSuccess: () => {
      setNotice("模型已给出映射建议，请核对目标类后校验");
      invalidate();
    },
  });
  const saveMapping = useMutation({
    mutationFn: () =>
      api<ImportJobSummary>(`/api/imports/${job.job_id}/mapping`, {
        method: "PATCH",
        body: JSON.stringify({
          files: draft.map((file) => ({
            stored_name: file.stored_name,
            target_class: file.target_class,
            entity_keys: file.entity_keys,
            time_field: file.time_field ?? null,
          })),
        }),
      }),
    onSuccess: () => {
      setNotice("映射已保存，请重新校验");
      invalidate();
    },
  });
  const validate = useMutation({
    mutationFn: () => api<ImportJobSummary>(`/api/imports/${job.job_id}/validate`, { method: "POST" }),
    onSuccess: (data) => {
      setNotice(data.validation?.passed ? "校验通过，可采纳入库" : "校验未通过，请查看错误");
      invalidate();
    },
  });
  const adopt = useMutation({
    mutationFn: () => api<ImportJobSummary>(`/api/imports/${job.job_id}/adopt`, { method: "POST" }),
    onSuccess: () => {
      setNotice("已采纳入库并通过线上门禁");
      invalidate();
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/imports/${job.job_id}`, { method: "DELETE" }),
    onSuccess: () => {
      setNotice("导入任务已丢弃");
      invalidate();
    },
  });

  const updateFile = (name: string, patch: Partial<ImportFileMapping>) =>
    setDraft((prev) => prev.map((file) => (file.stored_name === name ? { ...file, ...patch } : file)));
  const busy = analyze.isPending || saveMapping.isPending || validate.isPending || adopt.isPending || remove.isPending;
  const mutationError = analyze.error || saveMapping.error || validate.error || adopt.error || remove.error;
  const terminal = job.status === "adopted" || job.status === "rejected";

  return (
    <div className="catalog-card import-card">
      <div className="import-card-head">
        <div>
          <strong>{job.summary || job.job_id}</strong>
          <small className="import-sub">
            {new Date(job.created_at).toLocaleString()} · {job.classification}
          </small>
        </div>
        <span className={`import-badge import-badge-${job.status}`}>
          {IMPORT_STATUS_LABEL[job.status] || job.status}
        </span>
      </div>

      {restricted && (
        <div className="import-restricted">
          <LockKeyhole size={14} /> restricted 素材仅暂存与记录元数据，禁止采纳入库。
        </div>
      )}

      {job.uploaded && job.status === "uploaded" && (
        <ul className="import-filelist">
          {job.uploaded.map((file) => (
            <li key={file.stored_name}>
              {file.original_name}
              <small> · {file.format} · {(file.size_bytes / 1024).toFixed(1)} KB</small>
            </li>
          ))}
        </ul>
      )}

      {(job.manifest.staged_only || []).length > 0 && (
        <div className="import-hint">
          仅暂存（需人工转结构化后再导入）：
          {(job.manifest.staged_only || []).map((item) => item.stored_name).join("、")}
        </div>
      )}

      {draft.length > 0 && (
        <div className="import-mapping">
          {draft.map((file) => (
            <div key={file.stored_name} className="import-map-row">
              <div className="import-map-name">
                {file.stored_name}
                {file.derived_from && <small> · 由 {file.derived_from} 扁平化</small>}
              </div>
              <select
                value={file.target_class}
                disabled={terminal}
                onChange={(event) =>
                  updateFile(file.stored_name, { target_class: event.target.value, target_class_valid: true })
                }
              >
                <option value="">— 选择目标本体类 —</option>
                {classes.map((cls) => (
                  <option key={cls.iri} value={cls.iri}>
                    {cls.label}（{cls.iri.split(":").pop()}）
                  </option>
                ))}
              </select>
              <div className="import-keys">
                {file.headers.map((header) => (
                  <label key={header} className="import-key">
                    <input
                      type="checkbox"
                      disabled={terminal}
                      checked={file.entity_keys.includes(header)}
                      onChange={(event) =>
                        updateFile(file.stored_name, {
                          entity_keys: event.target.checked
                            ? [...file.entity_keys, header]
                            : file.entity_keys.filter((key) => key !== header),
                        })
                      }
                    />
                    {header}
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {job.validation?.error && <div className="import-error">{job.validation.error}</div>}
      {job.validation?.passed && (
        <div className="import-ok">
          <CheckCircle2 size={14} /> {job.validation.datasets?.length || 0} 个数据集通过门禁
        </div>
      )}
      {job.status === "adopted" && (job.adopted_paths.files || []).length > 0 && (
        <div className="import-hint">已入库：{(job.adopted_paths.files || []).join("、")}</div>
      )}
      {mutationError && <ErrorBox error={mutationError} />}

      <div className="import-actions">
        {job.status === "uploaded" && (
          <>
            <select value={model} onChange={(event) => setModel(event.target.value)}>
              {models.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.id}
                </option>
              ))}
            </select>
            <Button primary onClick={() => analyze.mutate()} disabled={busy}>
              <Sparkles size={14} />
              {analyze.isPending ? "分析中…" : "分析映射"}
            </Button>
          </>
        )}
        {(job.status === "analyzed" || job.status === "invalid" || job.status === "validated") && !restricted && (
          <>
            <Button onClick={() => saveMapping.mutate()} disabled={busy}>
              <Save size={14} /> 保存映射
            </Button>
            <Button primary onClick={() => validate.mutate()} disabled={busy}>
              <ShieldCheck size={14} />
              {validate.isPending ? "校验中…" : "校验"}
            </Button>
          </>
        )}
        {job.status === "validated" && !restricted && (
          <Button primary onClick={() => adopt.mutate()} disabled={busy}>
            <CheckCircle2 size={14} />
            {adopt.isPending ? "采纳中…" : "采纳入库"}
          </Button>
        )}
        {!terminal && (
          <Button danger onClick={() => remove.mutate()} disabled={busy}>
            <Trash2 size={14} /> 丢弃
          </Button>
        )}
      </div>
    </div>
  );
}

function Business() {
  const query = useQuery<{
    items: Array<{ path: string; name: string; document: unknown }>;
  }>({ queryKey: ["business"], queryFn: () => api("/api/business-models") });
  return (
    <CatalogPage
      title="经营模型"
      subtitle="成本、产能、周期与风险的可计算关系。"
      icon={<Gauge />}
      exportKind="business"
      metricKeys={["business_relations"]}
      query={query}
    >
      <BusinessAssistant />
    </CatalogPage>
  );
}

type DraftValidation = {
  passed: boolean;
  errors: string[];
  outputs: Record<string, { value: number; unit: string }>;
};

type DraftSummary = {
  draft_id: string;
  status: string;
  intent: string;
  domain: string;
  summary: string;
  llm_model_id: string;
  validation: DraftValidation;
  promoted_paths: Record<string, string>;
  created_at: string;
  approved_at?: string | null;
  rejected_at?: string | null;
};

const DRAFT_STATUS_LABEL: Record<string, string> = {
  drafted: "草拟中",
  validated: "校验通过",
  invalid: "校验未过",
  approved: "已采纳",
  rejected: "已丢弃",
};

type BaselineSchedule = {
  enabled: boolean;
  generate_time: string;
  timezone: string;
  daily_count: number;
  llm_model_id: string | null;
  domain_strategy: string;
  last_generated_date: string | null;
};

function BaselineScheduleSettings({ models }: { models: Array<{ id: string }> }) {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const schedule = useQuery<BaselineSchedule>({
    queryKey: ["baseline-schedule"],
    queryFn: () => api("/api/business-models/schedule"),
  });
  const [state, setState] = useState<BaselineSchedule | null>(null);
  useEffect(() => {
    if (schedule.data && !state) setState(schedule.data);
  }, [schedule.data, state]);
  const save = useMutation({
    mutationFn: () =>
      api("/api/business-models/schedule", {
        method: "PUT",
        body: JSON.stringify({
          enabled: state?.enabled ?? false,
          generate_time: state?.generate_time ?? "03:00",
          daily_count: state?.daily_count ?? 3,
          llm_model_id: state?.llm_model_id || null,
          domain_strategy: state?.domain_strategy || "gap_hotspot",
        }),
      }),
    onSuccess: () => {
      setNotice("定时起草设置已保存");
      queryClient.invalidateQueries({ queryKey: ["baseline-schedule"] });
    },
  });
  if (!state) return <Loading />;
  return (
    <Panel
      title="定时自动起草"
      meta="每日按缺口热点自动起草 + 校验，仍由人批量采纳落盘"
    >
      <div className="settings-form">
        <label className="switch-line">
          <span>启用每日自动起草</span>
          <input
            type="checkbox"
            checked={state.enabled}
            onChange={(e) => setState({ ...state, enabled: e.target.checked })}
          />
        </label>
        <label>
          起草时间
          <input
            type="time"
            value={state.generate_time}
            onChange={(e) => setState({ ...state, generate_time: e.target.value })}
          />
        </label>
        <label>
          每日份数
          <input
            type="number"
            min={1}
            max={10}
            value={state.daily_count}
            onChange={(e) =>
              setState({ ...state, daily_count: Number(e.target.value) })
            }
          />
        </label>
        <label>
          模型
          <select
            value={state.llm_model_id || ""}
            onChange={(e) =>
              setState({ ...state, llm_model_id: e.target.value || null })
            }
          >
            <option value="">默认模型</option>
            {models.map((item) => (
              <option key={item.id} value={item.id}>
                {item.id}
              </option>
            ))}
          </select>
        </label>
        <small>
          意图来自反向特征缺口热点，仅自动起草 + 引擎校验；数值假设由你在采纳前判断，落盘仍需人工批量采纳。
        </small>
        <div className="actions">
          <Button primary onClick={() => save.mutate()} disabled={save.isPending}>
            <Save size={15} />
            {save.isPending ? "保存中…" : "保存设置"}
          </Button>
        </div>
        {save.error && <ErrorBox error={save.error} />}
      </div>
    </Panel>
  );
}

function BusinessAssistant() {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const models = useQuery<{ items: Array<{ id: string }>; default_model_id?: string }>({
    queryKey: ["models"],
    queryFn: () => api("/api/models?refresh=true"),
    retry: false,
  });
  const drafts = useQuery<{ items: DraftSummary[] }>({
    queryKey: ["business-drafts"],
    queryFn: () => api("/api/business-models/drafts"),
  });
  const [intent, setIntent] = useState("");
  const [domain, setDomain] = useState("manufacturing");
  const [model, setModel] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!model && models.data?.default_model_id)
      setModel(models.data.default_model_id);
  }, [models.data?.default_model_id, model]);
  const generate = useMutation({
    mutationFn: () =>
      api<DraftSummary>("/api/business-models/draft", {
        method: "POST",
        body: JSON.stringify({
          intent,
          domain,
          model_id: model || models.data?.default_model_id || "",
        }),
      }),
    onSuccess: (data) => {
      setNotice(
        data.validation?.passed
          ? "草案已生成并通过引擎校验"
          : "草案已生成，但未通过引擎校验，请查看错误",
      );
      setIntent("");
      queryClient.invalidateQueries({ queryKey: ["business-drafts"] });
    },
  });
  const approveBatch = useMutation({
    mutationFn: () =>
      api<{ approved: number; results: Array<{ draft_id: string; ok: boolean; error?: string }> }>(
        "/api/business-models/drafts/approve-batch",
        {
          method: "POST",
          body: JSON.stringify({ draft_ids: Array.from(selected) }),
        },
      ),
    onSuccess: (data) => {
      const failed = data.results.filter((item) => !item.ok).length;
      setNotice(
        failed === 0
          ? `已批量采纳 ${data.approved} 份基线`
          : `采纳 ${data.approved} 份，${failed} 份失败`,
      );
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ["business-drafts"] });
      queryClient.invalidateQueries({ queryKey: ["business"] });
    },
  });
  const canGenerate = intent.trim().length >= 4 && !generate.isPending;
  const items = drafts.data?.items || [];
  const selectableIds = items
    .filter((draft) => draft.status === "validated" && draft.validation?.passed)
    .map((draft) => draft.draft_id);
  const toggleSelect = (draftId: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(draftId) ? next.delete(draftId) : next.add(draftId);
      return next;
    });
  const allSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(selectableIds));
  return (
    <Panel
      title="经营基线协作 Agent"
      meta="人触发起草 · 引擎门禁校验 · 人审批晋升"
      className="business-assistant"
    >
      <p>
        描述经营场景，LLM 起草完整三件套基线（模板 + 数据集 + 模型），经引擎真实门禁校验后，由你点『采纳为基线』落盘为线上可复用基线。
      </p>
      <textarea
        value={intent}
        onChange={(event) => setIntent(event.target.value)}
        placeholder="例如：某晶圆厂产线，评估良率提升 2% 与固定成本变化对单月利润的影响"
        rows={3}
      />
      <div className="actions">
        <input
          value={domain}
          onChange={(event) => setDomain(event.target.value)}
          placeholder="域，如 manufacturing / semiconductor.fab"
        />
        <select value={model} onChange={(event) => setModel(event.target.value)}>
          {(models.data?.items || []).map((item) => (
            <option key={item.id} value={item.id}>
              {item.id}
            </option>
          ))}
        </select>
        <Button primary onClick={() => generate.mutate()} disabled={!canGenerate}>
          <Sparkles size={15} />
          {generate.isPending ? "起草中…" : "生成草案"}
        </Button>
      </div>
      {generate.error && <ErrorBox error={generate.error} />}
      <BaselineScheduleSettings models={models.data?.items || []} />
      {drafts.isLoading ? (
        <Loading />
      ) : drafts.error ? (
        <ErrorBox error={drafts.error} />
      ) : items.length === 0 ? (
        <small>暂无草案。</small>
      ) : (
        <>
          {selectableIds.length > 0 && (
            <div className="actions">
              <label className="switch-line">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                />
                <span>全选校验通过的草案（{selectableIds.length}）</span>
              </label>
              <Button
                primary
                onClick={() => approveBatch.mutate()}
                disabled={selected.size === 0 || approveBatch.isPending}
              >
                <ShieldCheck size={15} />
                {approveBatch.isPending
                  ? "批量采纳中…"
                  : `批量采纳（${selected.size}）`}
              </Button>
            </div>
          )}
          {approveBatch.error && <ErrorBox error={approveBatch.error} />}
          <div className="catalog-grid">
            {items.map((draft) => (
              <BusinessDraftCard
                key={draft.draft_id}
                draft={draft}
                selectable={
                  draft.status === "validated" && !!draft.validation?.passed
                }
                selected={selected.has(draft.draft_id)}
                onToggleSelect={() => toggleSelect(draft.draft_id)}
              />
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

function BusinessDraftCard({
  draft,
  selectable = false,
  selected = false,
  onToggleSelect,
}: {
  draft: DraftSummary;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const [open, setOpen] = useState(false);
  const detail = useQuery<{
    documents: { template: unknown; dataset: unknown; model: unknown } | null;
  }>({
    queryKey: ["business-draft", draft.draft_id],
    queryFn: () =>
      api(`/api/business-models/drafts/${draft.draft_id}`),
    enabled: open,
  });
  const approve = useMutation({
    mutationFn: () =>
      api(`/api/business-models/drafts/${draft.draft_id}/approve`, {
        method: "POST",
      }),
    onSuccess: () => {
      setNotice("已采纳为线上经营基线");
      queryClient.invalidateQueries({ queryKey: ["business-drafts"] });
      queryClient.invalidateQueries({ queryKey: ["business"] });
    },
  });
  const reject = useMutation({
    mutationFn: () =>
      api(`/api/business-models/drafts/${draft.draft_id}/reject`, {
        method: "POST",
      }),
    onSuccess: () => {
      setNotice("草案已丢弃");
      queryClient.invalidateQueries({ queryKey: ["business-drafts"] });
    },
  });
  const validation = draft.validation || { passed: false, errors: [], outputs: {} };
  const outputs = Object.entries(validation.outputs || {});
  const actionable = draft.status === "validated" || draft.status === "invalid";
  return (
    <article className="catalog-card">
      <span><Gauge size={17} /></span>
      <div>
        <strong>
          {selectable && (
            <input
              type="checkbox"
              checked={selected}
              onChange={onToggleSelect}
              aria-label="选择此草案批量采纳"
            />
          )}{" "}
          {draft.summary || draft.intent}
        </strong>
        <small>
          {draft.domain} · {DRAFT_STATUS_LABEL[draft.status] || draft.status} ·{" "}
          {validation.passed ? "门禁通过" : "门禁未过"}
        </small>
      </div>
      {!validation.passed && (validation.errors || []).length > 0 && (
        <ul>
          {validation.errors.slice(0, 5).map((error, index) => (
            <li key={index}>{error}</li>
          ))}
        </ul>
      )}
      {validation.passed && outputs.length > 0 && (
        <pre>
          {outputs
            .map(([key, output]) => `${key}: ${output.value} ${output.unit}`)
            .join("\n")}
        </pre>
      )}
      {draft.status === "approved" &&
        Object.values(draft.promoted_paths || {}).length > 0 && (
          <small>已落盘：{Object.values(draft.promoted_paths).join("、")}</small>
        )}
      <div className="card-actions">
        <Button onClick={() => setOpen(!open)}>
          {open ? "收起三件套" : "查看三件套"}
        </Button>
        {actionable && (
          <>
            <Button
              primary
              onClick={() => approve.mutate()}
              disabled={!validation.passed || approve.isPending}
            >
              <ShieldCheck size={15} />
              {approve.isPending ? "采纳中…" : "采纳为基线"}
            </Button>
            <Button
              danger
              onClick={() => reject.mutate()}
              disabled={reject.isPending}
            >
              {reject.isPending ? "丢弃中…" : "丢弃"}
            </Button>
          </>
        )}
      </div>
      {(approve.error || reject.error) && (
        <ErrorBox error={approve.error || reject.error} />
      )}
      {open &&
        (detail.isLoading ? (
          <Loading />
        ) : detail.error ? (
          <ErrorBox error={detail.error} />
        ) : detail.data?.documents ? (
          <div className="draft-documents">
            {["template", "dataset", "model"].map((key) => (
              <details key={key} open>
                <summary>{key === "template" ? "模板 Template" : key === "dataset" ? "数据集 Dataset" : "模型 Model"}</summary>
                <pre className="document-content">
                  {JSON.stringify(
                    (detail.data!.documents as Record<string, unknown>)[key],
                    null,
                    2,
                  )}
                </pre>
              </details>
            ))}
          </div>
        ) : (
          <small>草案文件已不在磁盘（已晋升或已丢弃）。</small>
        ))}
    </article>
  );
}

function Simulation() {
  const query = useQuery<{
    items: Array<{ path: string; name: string; document: unknown }>;
  }>({ queryKey: ["simulations"], queryFn: () => api("/api/simulations") });
  return (
    <CatalogPage
      title="仿真引擎"
      subtitle="真实场景参数、经营结果与自动校验。"
      icon={<Cpu />}
      exportKind="simulation"
      metricKeys={["simulation_scenarios"]}
      query={query}
    />
  );
}

function CatalogPage({
  title,
  subtitle,
  icon,
  exportKind,
  metricKeys,
  query,
  children,
}: {
  title: string;
  subtitle: string;
  icon: React.ReactNode;
  exportKind: string;
  metricKeys: MetricKey[];
  query: {
    isLoading: boolean;
    error: unknown;
    data?: { items: Array<{ path: string; name: string; document: unknown }> };
  };
  children?: React.ReactNode;
}) {
  const metrics = useQuery<DashboardData["metrics"]>({
    queryKey: ["ontology-metrics"],
    queryFn: () => api("/api/ontology/metrics"),
    refetchInterval: 15000,
  });
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <ExportButton kind={exportKind} />
      </div>
      {metrics.data && (
        <MetricsOverview
          metrics={metrics.data}
          keys={metricKeys}
          className="metric-context"
        />
      )}
      {metrics.error && <ErrorBox error={metrics.error} />}
      {children}
      {query.isLoading ? (
        <Loading />
      ) : query.error ? (
        <ErrorBox error={query.error} />
      ) : (
        <div className="catalog-grid">
          {query.data?.items.map((item) => (
            <CatalogCard key={item.path} item={item} icon={icon} />
          ))}
        </div>
      )}
    </div>
  );
}

function CatalogCard({
  item,
  icon,
}: {
  item: { path: string; name: string; document: unknown };
  icon: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const detail = useQuery<{ content: string; size: number }>({
    queryKey: ["knowledge-file", item.path],
    queryFn: () =>
      api(`/api/knowledge/file?path=${encodeURIComponent(item.path)}`),
    enabled: open,
  });
  const preview = JSON.stringify(item.document, null, 2);
  return (
    <article className="catalog-card">
      <span>{icon}</span>
      <div>
        <strong>{item.name}</strong>
        <small>{item.path}</small>
      </div>
      <pre>
        {preview.length > 1000 ? `${preview.slice(0, 1000)}\n…` : preview}
      </pre>
      <div className="card-actions">
        <small>
          {preview.length} 字符{preview.length > 1000 ? "（当前为摘要）" : ""}
        </small>
        <Button onClick={() => setOpen(!open)}>
          {open ? "收起完整内容" : "查看完整内容"}
        </Button>
      </div>
      {open &&
        (detail.isLoading ? (
          <Loading />
        ) : detail.error ? (
          <ErrorBox error={detail.error} />
        ) : (
          <pre className="full-document">{detail.data?.content}</pre>
        ))}
    </article>
  );
}

function ScenarioKnowledgeProducts() {
  type Product = {
    path: string;
    name: string;
    size: number;
    updated_at: string;
    today_added: boolean;
  };
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const query = useQuery<{
    items: Product[];
    total: number;
    today_added: number;
  }>({
    queryKey: ["scenario-knowledge"],
    queryFn: () => api("/api/scenario-knowledge"),
    refetchInterval: 5000,
  });
  const selected =
    query.data?.items.find((item) => item.path === selectedPath) ||
    query.data?.items[0];
  const detail = useQuery<{ path: string; content: string; size: number }>({
    queryKey: ["scenario-knowledge-file", selected?.path],
    queryFn: () =>
      api(
        `/api/scenario-knowledge/file?path=${encodeURIComponent(selected!.path)}`,
      ),
    enabled: Boolean(selected?.path),
  });
  return (
    <Panel
      title="场景知识产物"
      meta={
        query.data
          ? `${query.data.total} 项 · 今日新增 ${query.data.today_added}`
          : "读取中"
      }
    >
      {query.isLoading ? (
        <Loading text="正在读取场景知识产物…" />
      ) : query.error ? (
        <ErrorBox error={query.error} />
      ) : !query.data?.items.length ? (
        <div className="reference-empty">
          流程完成后，场景知识产物会出现在这里
        </div>
      ) : (
        <div className="scenario-products">
          <div className="scenario-product-list">
            {query.data.items.map((item) => (
              <button
                className={`scenario-product-item ${selected?.path === item.path ? "selected" : ""}`}
                key={item.path}
                onClick={() => setSelectedPath(item.path)}
              >
                <span className="scenario-product-icon">
                  <FileText size={15} />
                </span>
                <span>
                  <strong>{item.name}</strong>
                  <small>{item.path}</small>
                  <em>
                    {(item.size / 1024).toFixed(1)} KB ·{" "}
                    {new Date(item.updated_at).toLocaleString()}
                    {item.today_added ? " · 今日新增" : ""}
                  </em>
                </span>
              </button>
            ))}
          </div>
          <div className="scenario-product-detail">
            {detail.isLoading ? (
              <Loading text="正在读取完整内容…" />
            ) : detail.error ? (
              <ErrorBox error={detail.error} />
            ) : (
              <>
                <div className="scenario-product-detail-head">
                  <strong>{selected?.name}</strong>
                  <small>{detail.data?.size.toLocaleString()} 字符</small>
                </div>
                <pre>{detail.data?.content}</pre>
              </>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

function Scenarios() {
  type ArticleItem = {
    id: number;
    title: string;
    subtitle: string;
    status: string;
    generation_stage?: string;
    generation_progress?: number;
    generation_error?: string | null;
    repair_attempts?: number;
    content_markdown: string;
    content_html: string;
    validation: { passed: boolean; errors: string[]; repair_attempts?: number; repair_limit?: number };
    word_count: number;
    assets: Array<{
      id: number;
      file_path: string;
      mime_type: string;
      caption: string;
    }>;
    wechat_status?: string;
    wechat_draft_media_id?: string | null;
    wechat_last_error?: string | null;
    wechat_sent_at?: string | null;
  };
  type ArticleSettings = {
    enabled: boolean;
    generate_time: string;
    timezone: string;
    approval_required: boolean;
    auto_visuals: boolean;
    daily_article_count: number;
    article_model_id?: string | null;
    image_model_id?: string | null;
    image_count: number;
    auto_repair: boolean;
    max_repair_attempts: number;
  };
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const articles = useQuery<{ items: ArticleItem[] }>({
    queryKey: ["articles"],
    queryFn: () => api("/api/articles"),
    refetchInterval: 5000,
  });
  const metrics = useQuery<DashboardData["metrics"]>({
    queryKey: ["ontology-metrics"],
    queryFn: () => api("/api/ontology/metrics"),
    refetchInterval: 15000,
  });
  const settingsQuery = useQuery<ArticleSettings>({
    queryKey: ["article-settings"],
    queryFn: () => api("/api/article-settings"),
  });
  const models = useQuery<{ items: Array<{ id: string }>; total: number }>({
    queryKey: ["article-models"],
    queryFn: () => api("/api/models?refresh=true"),
    retry: false,
  });
  const [settingsState, setSettingsState] = useState<ArticleSettings | null>(
    null,
  );
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  useEffect(() => {
    if (settingsQuery.data) setSettingsState(settingsQuery.data);
  }, [settingsQuery.data]);
  useEffect(() => {
    const selected =
      articles.data?.items.find((item) => item.id === selectedId) ||
      articles.data?.items[0];
    if (selected) {
      setSelectedId(selected.id);
      setTitle(selected.title);
      setContent(selected.content_markdown);
    }
  }, [articles.data?.items, selectedId]);
  const selected = articles.data?.items.find((item) => item.id === selectedId);
  const generate = useMutation({
    mutationFn: () => api("/api/article-settings/run-now", { method: "POST" }),
    onSuccess: () => {
      setNotice("文章批量生成任务已启动");
      queryClient.invalidateQueries({ queryKey: ["articles"] });
    },
  });
  const saveSettings = useMutation({
    mutationFn: () =>
      api("/api/article-settings", {
        method: "PUT",
        body: JSON.stringify(settingsState),
      }),
    onSuccess: () => {
      setNotice("文章计划已保存");
      queryClient.invalidateQueries({ queryKey: ["article-settings"] });
    },
  });
  const saveArticle = useMutation({
    mutationFn: () =>
      api(`/api/articles/${selectedId}`, {
        method: "PATCH",
        body: JSON.stringify({
          title,
          content_markdown: content,
          revision_note: "前端草稿箱编辑",
        }),
      }),
    onSuccess: () => {
      setNotice("文章草稿已保存");
      queryClient.invalidateQueries({ queryKey: ["articles"] });
    },
  });
  const deleteArticle = useMutation({
    mutationFn: () => api(`/api/articles/${selectedId}`, { method: "DELETE" }),
    onSuccess: () => {
      setSelectedId(null);
      setNotice("文章已移入回收站");
      queryClient.invalidateQueries({ queryKey: ["articles"] });
    },
  });
  const sendWechat = useMutation({
    mutationFn: () =>
      api(`/api/articles/${selectedId}/wechat-draft`, { method: "POST" }),
    onSuccess: () => {
      setNotice("公众号草稿已创建，可在微信公众平台草稿箱查看");
      queryClient.invalidateQueries({ queryKey: ["articles"] });
    },
  });
  const setField = <K extends keyof ArticleSettings>(
    key: K,
    value: ArticleSettings[K],
  ) =>
    setSettingsState((state) => (state ? { ...state, [key]: value } : state));
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>公众号工作台</h2>
          <p>系统按计划自动生成文章并保存到草稿箱；每篇文章只聚焦一个场景。</p>
        </div>
        <div className="actions">
          <Button
            primary
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
          >
            {generate.isPending ? (
              <LoaderCircle className="status-spin" size={15} />
            ) : (
              <Sparkles size={15} />
            )}
            {generate.isPending ? "生成中…" : "立即生成今日文章"}
          </Button>
          <ExportButton kind="scenarios" label="导出场景知识产物" />
        </div>
      </div>
      {(generate.error ||
        saveSettings.error ||
        saveArticle.error ||
        deleteArticle.error ||
        sendWechat.error) && (
        <ErrorBox
          error={
            generate.error ||
            saveSettings.error ||
            saveArticle.error ||
            deleteArticle.error ||
            sendWechat.error
          }
        />
      )}
      {metrics.data && (
        <MetricsOverview
          metrics={metrics.data}
          keys={["scenario_articles"]}
          className="metric-context"
        />
      )}
      {metrics.error && <ErrorBox error={metrics.error} />}
      <ScenarioKnowledgeProducts />
      <div className="article-stats">
        <div>
          <span>今日计划</span>
          <strong>{settingsState?.daily_article_count || 1} 篇</strong>
        </div>
        <div>
          <span>草稿箱</span>
          <strong>{articles.data?.items.length || 0} 篇</strong>
        </div>
        <div>
          <span>待审核</span>
          <strong>
            {articles.data?.items.filter(
              (item) => item.status === "waiting_approval",
            ).length || 0}{" "}
            篇
          </strong>
        </div>
        <div>
          <span>图片模型</span>
          <strong>{settingsState?.image_model_id || "未指定"}</strong>
        </div>
      </div>
      <Panel
        title="文章生成任务"
        meta={`${articles.data?.items.length || 0} 条`}
      >
        <div className="article-task-list">
          {articles.isLoading ? (
            <Loading text="正在读取文章生成任务…" />
          ) : articles.data?.items.length ? (
            articles.data.items.slice(0, 20).map((item) => (
              <div className="article-task-item" key={`task-${item.id}`}>
                <div>
                  <strong>{item.title}</strong>
                  <small>
                    {item.generation_stage || item.status}
                    {item.generation_error ? ` · ${item.generation_error}` : ""}
                  </small>
                </div>
                <progress
                  value={
                    item.generation_progress ??
                    (item.status === "generating" ? 0 : 100)
                  }
                  max={100}
                />
              </div>
            ))
          ) : (
            <div className="empty">尚无文章生成任务</div>
          )}
        </div>
      </Panel>
      <div className="article-workspace">
        <Panel title="自动生成计划" meta="无需手动发现主题">
          <div className="settings-form">
            {settingsState ? (
              <>
                <label className="switch-line">
                  <span>启用每日自动生成</span>
                  <input
                    type="checkbox"
                    checked={settingsState.enabled}
                    onChange={(e) => setField("enabled", e.target.checked)}
                  />
                </label>
                <label>
                  生成时间
                  <input
                    type="time"
                    value={settingsState.generate_time}
                    onChange={(e) => setField("generate_time", e.target.value)}
                  />
                </label>
                <label>
                  每天生成数量
                  <input
                    type="number"
                    min="1"
                    max="10"
                    value={settingsState.daily_article_count}
                    onChange={(e) =>
                      setField("daily_article_count", Number(e.target.value))
                    }
                  />
                </label>
                <label>
                  文章模型
                  <select
                    value={settingsState.article_model_id || ""}
                    onChange={(e) =>
                      setField("article_model_id", e.target.value || null)
                    }
                  >
                    <option value="">跟随默认模型</option>
                    {models.data?.items.map((item) => (
                      <option key={item.id}>{item.id}</option>
                    ))}
                  </select>
                </label>
                <label>
                  生图模型
                  <select
                    value={settingsState.image_model_id || ""}
                    onChange={(e) =>
                      setField("image_model_id", e.target.value || null)
                    }
                  >
                    <option value="">不调用图片模型（使用 SVG 兜底）</option>
                    {models.data?.items.map((item) => (
                      <option key={item.id}>{item.id}</option>
                    ))}
                  </select>
                </label>
                <label>
                  每篇配图数量
                  <input
                    type="number"
                    min="0"
                    max="5"
                    value={settingsState.image_count}
                    onChange={(e) =>
                      setField("image_count", Number(e.target.value))
                    }
                  />
                </label>
                <label className="switch-line">
                  <span>自动返修校验失败文章</span>
                  <input
                    type="checkbox"
                    checked={settingsState.auto_repair}
                    onChange={(e) => setField("auto_repair", e.target.checked)}
                  />
                </label>
                {settingsState.auto_repair && (
                  <label>
                    最大自动返修次数（最多 3 次）
                    <input
                      type="number"
                      min="0"
                      max="3"
                      value={settingsState.max_repair_attempts}
                      onChange={(e) =>
                        setField(
                          "max_repair_attempts",
                          Math.min(3, Math.max(0, Number(e.target.value))),
                        )
                      }
                    />
                  </label>
                )}
                <label className="switch-line">
                  <span>需要人工审核</span>
                  <input
                    type="checkbox"
                    checked={settingsState.approval_required}
                    onChange={(e) =>
                      setField("approval_required", e.target.checked)
                    }
                  />
                </label>
                <label className="switch-line">
                  <span>自动生成封面和插图</span>
                  <input
                    type="checkbox"
                    checked={settingsState.auto_visuals}
                    onChange={(e) => setField("auto_visuals", e.target.checked)}
                  />
                </label>
                <Button
                  primary
                  onClick={() => saveSettings.mutate()}
                  disabled={saveSettings.isPending}
                >
                  {saveSettings.isPending ? "保存中…" : "保存生成计划"}
                </Button>
              </>
            ) : (
              <Loading />
            )}
          </div>
        </Panel>
        <Panel title="草稿箱" meta={`${articles.data?.items.length || 0} 篇`}>
          <div className="draft-list">
            {articles.isLoading ? (
              <Loading />
            ) : articles.data?.items.length ? (
              articles.data.items.map((item) => (
                <button
                  className={`draft-item ${item.id === selectedId ? "selected" : ""}`}
                  key={item.id}
                  onClick={() => setSelectedId(item.id)}
                >
                  <span className={`status-dot ${item.status}`} />
                  <span>
                    <strong>{item.title}</strong>
                    <small>
                      {item.generation_stage ||
                        `${item.word_count} 字 · ${item.status}`}
                      {item.wechat_status === "sent_to_draft"
                        ? " · 已发公众号草稿"
                        : ""}
                    </small>
                    {item.status === "generating" && (
                      <progress
                        className="article-task-progress"
                        value={item.generation_progress || 0}
                        max={100}
                      />
                    )}
                  </span>
                </button>
              ))
            ) : (
              <div className="empty">按计划生成后，文章会出现在这里</div>
            )}
          </div>
        </Panel>
      </div>
      <div className="article-editor-grid">
        <Panel
          title={selected ? "编辑草稿" : "选择一篇草稿"}
          meta={selected ? `文章 #${selected.id}` : "草稿箱"}
        >
          {selected ? (
            <div className="article-editor">
              <label>
                公众号标题
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
              </label>
              <label>
                Markdown 正文
                <textarea
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                />
              </label>
              <div className="editor-actions">
                <Button
                  primary
                  onClick={() => saveArticle.mutate()}
                  disabled={saveArticle.isPending}
                >
                  {saveArticle.isPending ? "保存中…" : "保存草稿"}
                </Button>
                <Button
                  onClick={() => sendWechat.mutate()}
                  disabled={
                    sendWechat.isPending ||
                    selected.wechat_status === "sent_to_draft" ||
                    !selected.validation?.passed
                  }
                >
                  {sendWechat.isPending ? (
                    <>
                      <LoaderCircle className="status-spin" size={15} />
                      发送中…
                    </>
                  ) : selected.wechat_status === "sent_to_draft" ? (
                    <>
                      <CheckCircle2 size={15} />
                      已发送到公众号草稿箱
                    </>
                  ) : (
                    <>
                      <Send size={15} />
                      发送到公众号草稿箱
                    </>
                  )}
                </Button>
                <Button
                  danger
                  onClick={() => deleteArticle.mutate()}
                  disabled={deleteArticle.isPending}
                >
                  {deleteArticle.isPending ? "删除中…" : "删除文章"}
                </Button>
              </div>
              {!selected.validation?.passed && (
                <div className="article-validation-error" role="alert">
                  <strong>自动校验未通过</strong>
                  <span>
                    已自动返修 {selected.repair_attempts ?? selected.validation?.repair_attempts ?? 0}/{selected.validation?.repair_limit ?? 3} 次，仍需你修改正文。
                  </span>
                  <ul>
                    {(selected.validation?.errors || ["未返回具体校验原因"]).map(
                      (error) => <li key={error}>{error}</li>,
                    )}
                  </ul>
                </div>
              )}
              {selected.wechat_status === "failed" && (
                <div className="wechat-send-error">
                  上次发送失败：
                  {selected.wechat_last_error || "请检查公众号凭据和网络配置"}
                </div>
              )}
              {selected.wechat_status === "sent_to_draft" && (
                <div className="wechat-send-success">
                  <CheckCircle2 size={14} />
                  已创建微信草稿
                  {selected.wechat_sent_at
                    ? ` · ${new Date(selected.wechat_sent_at).toLocaleString()}`
                    : ""}
                </div>
              )}
            </div>
          ) : (
            <div className="empty">从左侧草稿箱选择文章开始编辑</div>
          )}
        </Panel>
        <Panel
          title="公众号预览"
          meta={
            selected
              ? `${selected.validation?.passed ? "校验通过" : "需要修改"} · ${selected.assets?.length || 0} 张图`
              : "实时预览"
          }
        >
          {selected ? (
            <div className="article-preview">
              {selected.assets?.length &&
              !selected.content_html.includes("<img") ? (
                <div className="article-images">
                  {selected.assets.map((asset) => (
                    <img
                      key={asset.id}
                      src={`/api/articles/${selected.id}/assets/${asset.id}`}
                      alt={asset.caption}
                      loading="lazy"
                    />
                  ))}
                </div>
              ) : null}
              <div
                dangerouslySetInnerHTML={{ __html: selected.content_html }}
              />
            </div>
          ) : (
            <div className="empty">编辑后保存，再查看排版效果</div>
          )}
        </Panel>
      </div>
    </div>
  );
}

const reportStatusLabels: Record<string, string> = {
  generating: "生成中",
  validating: "校验中",
  waiting_approval: "待审核",
  approved: "已审核",
  sending: "发送中",
  sent: "已发送",
  send_failed: "发送失败",
  send_blocked: "发送受阻",
};

function ReportStatusBadge({ status }: { status?: string }) {
  if (!status)
    return (
      <span className="report-status-badge report-status-idle">尚未生成</span>
    );
  const label = reportStatusLabels[status] || status;
  const icon =
    status === "generating" ||
    status === "validating" ||
    status === "sending" ? (
      <LoaderCircle className="status-spin" size={14} />
    ) : status === "waiting_approval" ? (
      <ShieldCheck size={14} />
    ) : status === "sent" || status === "approved" ? (
      <CheckCircle2 size={14} />
    ) : (
      <AlertTriangle size={14} />
    );
  return (
    <span
      className={`report-status-badge report-status-${status}`}
      role="status"
      aria-live="polite"
    >
      {icon}
      {label}
    </span>
  );
}

function Reports({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const date = new Date().toLocaleDateString("sv-SE");
  const report = useQuery<{
    id: number;
    status: string;
    approval_required: boolean;
    content: string;
    validation: { passed: boolean; errors: string[] };
  }>({
    queryKey: ["report", date],
    queryFn: () => api(`/api/reports/daily/${date}`),
    retry: false,
    // 日报由后台异步生成，生成耗时可能超过一次性延迟；仅在处理中轮询，完成后自动停止。
    refetchInterval: (current) => {
      const status = current.state.data?.status;
      return status === "generating" ||
        status === "validating" ||
        status === "sending"
        ? 1500
        : false;
    },
  });
  const settingsQuery = useQuery<{
    enabled: boolean;
    generate_time: string;
    approval_required: boolean;
    reminder_timeout_minutes: number;
    email_sender?: string;
    email_recipient?: string;
    email_reminder_enabled: boolean;
  }>({
    queryKey: ["report-settings"],
    queryFn: () => api("/api/report-settings"),
  });
  const [content, setContent] = useState("");
  const [settingsState, setSettingsState] = useState(settingsQuery.data);
  useEffect(() => {
    if (report.data?.content) setContent(report.data.content);
  }, [report.data?.content]);
  useEffect(() => {
    if (settingsQuery.data) setSettingsState(settingsQuery.data);
  }, [settingsQuery.data]);
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["report", date] });
  const generate = useMutation({
    mutationFn: () =>
      api<{ id: number; status: string }>(
        `/api/reports/daily/${date}/generate`,
        { method: "POST" },
      ),
    onSuccess: (data) => {
      setNotice("日报生成任务已启动，完成后会自动刷新");
      queryClient.setQueryData(
        ["report", date],
        (previous: object | undefined) => ({
          ...(previous || {}),
          id: data.id,
          status: data.status,
        }),
      );
      refresh();
    },
  });
  const save = useMutation({
    mutationFn: () =>
      api(`/api/reports/daily/${date}`, {
        method: "PATCH",
        body: JSON.stringify({ content }),
      }),
    onSuccess: () => { setNotice("日报草稿已保存"); refresh(); },
  });
  const approve = useMutation({
    mutationFn: () =>
      api(`/api/reports/daily/${date}/approve`, { method: "POST" }),
    onSuccess: () => { setNotice("日报已审核通过"); refresh(); },
  });
  const send = useMutation({
    mutationFn: () =>
      api(`/api/reports/daily/${date}/send`, { method: "POST" }),
    onSuccess: () => { setNotice("日报已发送到企业微信"); refresh(); },
  });
  const updateSettings = useMutation({
    mutationFn: () =>
      api("/api/report-settings", {
        method: "PUT",
        body: JSON.stringify(settingsState),
      }),
    onSuccess: () => { setNotice("日报设置已保存"); queryClient.invalidateQueries({ queryKey: ["report-settings"] }); },
  });
  const error =
    generate.error ||
    save.error ||
    approve.error ||
    send.error ||
    updateSettings.error;
  const currentReportStatus = generate.isPending
    ? "generating"
    : report.data?.status;
  const processing = ["generating", "validating", "sending"].includes(
    currentReportStatus || "",
  );
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>日报中心</h2>
          <p>展示今日新增与当前总量，并沉淀场景知识产物摘要。</p>
        </div>
        <div className="actions">
          <Button onClick={() => generate.mutate()} disabled={processing}>
            <Sparkles size={15} />
            {generate.isPending ? "启动中…" : processing ? "日报生成中…" : "写日报"}
          </Button>
          <Button
            onClick={() => save.mutate()}
            disabled={!content || processing || save.isPending}
          >
            <Save size={15} />
            {save.isPending ? "保存中…" : "保存"}
          </Button>
          {report.data?.approval_required && (
            <Button
              onClick={() => approve.mutate()}
              disabled={
                !report.data || report.data.status === "approved" || processing || approve.isPending
              }
            >
              <ShieldCheck size={15} />
              {approve.isPending ? "审核中…" : "审核通过"}
            </Button>
          )}
          <Button
            primary
            onClick={() => send.mutate()}
            disabled={!report.data || processing || send.isPending}
          >
            <Send size={15} />
            {send.isPending ? "发送中…" : "发送企微"}
          </Button>
        </div>
      </div>
      {error && <ErrorBox error={error} />}
      <div
        className={`report-status-panel ${currentReportStatus ? `report-status-panel-${currentReportStatus}` : "report-status-panel-idle"}`}
      >
        <ReportStatusBadge status={currentReportStatus} />
        <span>
          {currentReportStatus === "generating"
            ? "模型正在整理最新指标，完成后内容会自动显示在下方编辑框。"
            : currentReportStatus === "waiting_approval"
              ? "日报已生成并通过校验，请审核后发送。"
              : currentReportStatus === "sent"
                ? "日报已发送到企业微信。"
                : currentReportStatus === "send_blocked"
                  ? "日报未通过校验或缺少发送配置，请查看错误提示。"
                  : "点击“写日报”后，系统会自动跟踪后台生成状态。"}
        </span>
      </div>
      <div className="report-grid">
        <Panel
          title={`${date} 日报`}
          meta={<ReportStatusBadge status={currentReportStatus} />}
        >
          {report.isLoading ? (
            <Loading />
          ) : (
            <textarea
              className="report-editor"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="点击“写日报”生成Markdown草稿"
            />
          )}
        </Panel>
        <Panel title="定时、审核与提醒" meta={user.preferences.timezone}>
          <div className="settings-form">
            {settingsState ? (
              <>
                <label className="switch-line">
                  <span>自动生成日报</span>
                  <input
                    type="checkbox"
                    checked={settingsState.enabled}
                    onChange={(e) =>
                      setSettingsState({
                        ...settingsState,
                        enabled: e.target.checked,
                      })
                    }
                  />
                </label>
                <label>
                  生成时间
                  <input
                    type="time"
                    value={settingsState.generate_time}
                    onChange={(e) =>
                      setSettingsState({
                        ...settingsState,
                        generate_time: e.target.value,
                      })
                    }
                  />
                </label>
                <label className="switch-line">
                  <span>需要审核人审核（默认开启）</span>
                  <input
                    type="checkbox"
                    checked={settingsState.approval_required}
                    onChange={(e) =>
                      setSettingsState({
                        ...settingsState,
                        approval_required: e.target.checked,
                      })
                    }
                  />
                </label>
                {!settingsState.approval_required && (
                  <div className="warning-box">
                    关闭后，模型写完并通过自动校验会直接发送企业微信，无需再次点击。
                  </div>
                )}
                <label className="switch-line">
                  <span>发送邮箱提醒（默认开启）</span>
                  <input
                    type="checkbox"
                    checked={settingsState.email_reminder_enabled}
                    onChange={(e) =>
                      setSettingsState({
                        ...settingsState,
                        email_reminder_enabled: e.target.checked,
                      })
                    }
                  />
                </label>
                {!settingsState.email_reminder_enabled && (
                  <div className="warning-box">
                    关闭后，日报超时待审核不再发送邮件提醒；企业微信发送与审核流程不受影响。
                  </div>
                )}
                <label>
                  未发送提醒（分钟）
                  <input
                    type="number"
                    min="1"
                    value={settingsState.reminder_timeout_minutes}
                    onChange={(e) =>
                      setSettingsState({
                        ...settingsState,
                        reminder_timeout_minutes: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  QQ发件邮箱
                  <input
                    value={settingsState.email_sender || ""}
                    onChange={(e) =>
                      setSettingsState({
                        ...settingsState,
                        email_sender: e.target.value,
                      })
                    }
                  />
                </label>
                <label>
                  提醒收件邮箱
                  <input
                    value={settingsState.email_recipient || ""}
                    onChange={(e) =>
                      setSettingsState({
                        ...settingsState,
                        email_recipient: e.target.value,
                      })
                    }
                  />
                </label>
                <Button primary onClick={() => updateSettings.mutate()} disabled={updateSettings.isPending}>
                  {updateSettings.isPending ? "保存中…" : "保存日报设置"}
                </Button>
              </>
            ) : (
              <Loading />
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

const ACTIVE_RUN_STATUSES = [
  "pending",
  "running",
  "recovering",
  "paused",
  "between_rounds",
  "stopping_after_round",
  "cancelling",
];

function RunHistory() {
  const [selected, setSelected] = useState<string | null>(null);
  const setNotice = useAppStore((state) => state.setNotice);
  const query = useQuery<{ items: RunInfo[] }>({
    queryKey: ["runs"],
    queryFn: () => api("/api/runs"),
  });
  const finishedCount = (query.data?.items || []).filter(
    (run) => !ACTIVE_RUN_STATUSES.includes(run.status),
  ).length;
  const removeRun = useMutation({
    mutationFn: (id: string) => api(`/api/runs/${id}`, { method: "DELETE" }),
    onSuccess: (_result, id) => {
      setNotice("运行历史已删除");
      if (selected === id) setSelected(null);
      query.refetch();
    },
  });
  const clearFinished = useMutation({
    mutationFn: () => api<{ deleted: number }>("/api/runs/clear-finished", { method: "POST" }),
    onSuccess: (result) => {
      setNotice(`已清空 ${result.deleted} 条非运行中的历史`);
      setSelected(null);
      query.refetch();
    },
  });
  const rounds = useQuery<{
    items: Array<{
      round_number: number;
      status: string;
      current_stage: string;
      duration_seconds?: number;
      error?: string;
      checkpoint_id?: string;
      resumed_count: number;
      node_attempts: Record<string, number>;
      quarantined_files: string[];
      metrics_before: Record<string, number>;
      metrics_after: Record<string, number>;
      artifacts: { evidence_pages?: number; scenario_articles?: number; repair_attempts?: number; repair_successes?: number; repair_failures?: number; candidate_counts?: Record<string, number>; partial?: boolean };
    }>;
  }>({
    queryKey: ["run-rounds", selected],
    queryFn: () => api(`/api/runs/${selected}/rounds`),
    enabled: Boolean(selected),
    refetchInterval: selected ? 3000 : false,
  });
  const references = useQuery<{ items: RunReference[]; total: number }>({
    queryKey: ["run-references", selected],
    queryFn: () => api(`/api/runs/${selected}/references`),
    enabled: Boolean(selected),
    refetchInterval: selected ? 3000 : false,
  });
  const retry = useMutation({
    mutationFn: (id: string) =>
      api(`/api/runs/${id}/retry`, { method: "POST" }),
    onSuccess: () => { setNotice("任务已重新启动"); query.refetch(); },
  });
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>运行历史</h2>
          <p>
            每个持续任务包含多轮记录，每轮、节点 checkpoint 和每个 Agent
            产物均独立保存。
          </p>
        </div>
        <Button
          danger
          disabled={finishedCount === 0 || clearFinished.isPending}
          onClick={() => {
            if (window.confirm(`将永久删除 ${finishedCount} 条非运行中的历史（含轮次、事件与产物），运行中的任务保留。确认清空？`)) {
              clearFinished.mutate();
            }
          }}
        >
          <Trash2 size={15} />
          {clearFinished.isPending ? "清空中…" : `一键清空已结束（${finishedCount}）`}
        </Button>
      </div>
      {(removeRun.error || clearFinished.error) && (
        <ErrorBox error={removeRun.error || clearFinished.error} />
      )}
      <Panel title="持续任务" meta={`${query.data?.items.length || 0} 条`}>
        {query.isLoading ? (
          <Loading />
        ) : query.error ? (
          <ErrorBox error={query.error} />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>运行ID</th>
                  <th>状态</th>
                  <th>当前/完成轮次</th>
                  <th>编排器</th>
                  <th>模型</th>
                  <th>阶段</th>
                  <th>Agent</th>
                  <th>发布</th>
                  <th>时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {query.data?.items.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <button
                        className="link-button"
                        onClick={() => setSelected(run.id)}
                      >
                        {run.id}
                      </button>
                    </td>
                    <td>
                      <span className={`status-tag ${run.status}`}>
                        {run.status}
                      </span>
                    </td>
                    <td>
                      {run.current_round} / {run.rounds_completed}
                    </td>
                    <td>
                      <span className="engine-tag">
                        {run.orchestrator_engine}/{run.checkpoint_backend}
                      </span>
                    </td>
                    <td>{run.model_id}</td>
                    <td>
                      {stageLabels[run.current_stage] || run.current_stage}
                    </td>
                    <td>{run.agents.length}</td>
                    <td>{run.publish_changes ? "自动发布" : "仅候选"}</td>
                    <td>{new Date(run.created_at).toLocaleString()}</td>
                    <td>
                      <div className="row-actions">
                        <Button onClick={() => retry.mutate(run.id)} disabled={retry.isPending}>
                          {retry.isPending ? "启动中…" : "重新启动"}
                        </Button>
                        <Button
                          danger
                          className="icon-button"
                          aria-label="删除该运行历史"
                          title={
                            ACTIVE_RUN_STATUSES.includes(run.status)
                              ? "运行中的任务需先停止才能删除"
                              : "删除该运行历史"
                          }
                          disabled={
                            ACTIVE_RUN_STATUSES.includes(run.status) ||
                            removeRun.isPending
                          }
                          onClick={() => {
                            if (window.confirm(`删除 ${run.id} 及其全部轮次、事件与产物？此操作不可恢复。`)) {
                              removeRun.mutate(run.id);
                            }
                          }}
                        >
                          <Trash2 size={14} />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      {selected && (
        <>
          <Panel
            title={`${selected} · 轮次明细`}
            meta={`${rounds.data?.items.length || 0} 轮`}
          >
            {rounds.isLoading ? (
              <Loading />
            ) : rounds.error ? (
              <ErrorBox error={rounds.error} />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>轮次</th>
                      <th>状态</th>
                      <th>阶段</th>
                      <th>Checkpoint</th>
                      <th>恢复</th>
                      <th>节点执行</th>
                      <th>隔离</th>
                      <th>网页证据</th>
                      <th>场景知识产物</th>
                      <th>自动返修</th>
                      <th>耗时</th>
                      <th>错误</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rounds.data?.items.map((round) => (
                      <tr key={round.round_number}>
                        <td>第 {round.round_number} 轮</td>
                        <td>
                          <span className={`status-tag ${round.status}`}>
                            {round.status}
                          </span>
                        </td>
                        <td>
                          {stageLabels[round.current_stage] ||
                            round.current_stage}
                        </td>
                        <td>
                          <code>
                            {round.checkpoint_id?.slice(0, 12) || "-"}
                          </code>
                        </td>
                        <td>{round.resumed_count || 0}</td>
                        <td>
                          {Object.values(round.node_attempts || {}).reduce(
                            (sum, value) => sum + value,
                            0,
                          )}
                        </td>
                        <td>{round.quarantined_files?.length || 0}</td>
                        <td>{round.artifacts.evidence_pages || 0}</td>
                        <td>{round.artifacts.scenario_articles || 0}</td>
                        <td>{round.artifacts.repair_attempts || 0} 次（{round.artifacts.repair_successes || 0} 成功）</td>
                        <td>
                          {round.duration_seconds
                            ? `${round.duration_seconds}s`
                            : "-"}
                        </td>
                        <td>{round.error || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
          <Panel
            title="网页参考资料"
            meta={references.data ? `${references.data.total} 条` : "读取中"}
          >
            <ReferenceList
              items={references.data?.items}
              total={references.data?.total}
              isLoading={references.isLoading}
              hasWebSource
            />
          </Panel>
        </>
      )}
    </div>
  );
}

function SettingsPage({
  user,
  onUserChange,
}: {
  user: User;
  onUserChange: (user: User) => void;
}) {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const [values, setValues] = useState<Record<string, string>>({
    llm_api_key: "",
    wecom_webhook_key: "",
    qq_smtp_auth_code: "",
    wechat_app_id: "",
    wechat_app_secret: "",
    wecom_aibot_id: "",
    wecom_aibot_secret: "",
  });
  const credentials = useQuery<{
    items: Array<{ kind: string; configured: boolean; masked_hint: string }>;
  }>({ queryKey: ["credentials"], queryFn: () => api("/api/credentials") });
  const save = useMutation({
    mutationFn: ({ kind, value }: { kind: string; value: string }) =>
      api("/api/credentials", {
        method: "PUT",
        body: JSON.stringify({ kind, value }),
      }),
    onSuccess: (_, variables) => {
      setValues((items) => ({ ...items, [variables.kind]: "" }));
      queryClient.invalidateQueries({ queryKey: ["credentials"] });
      queryClient.invalidateQueries({ queryKey: ["models"] });
      setNotice(`${labels[variables.kind] || "凭据"}已保存`);
    },
  });
  const remove = useMutation({
    mutationFn: (kind: string) =>
      api(`/api/credentials/${kind}`, { method: "DELETE" }),
    onSuccess: (_, kind) => { setNotice(`${labels[kind] || "凭据"}已清除`); queryClient.invalidateQueries({ queryKey: ["credentials"] }); },
  });
  const testWecom = useMutation({
    mutationFn: () =>
      api("/api/report-settings/test-wecom", { method: "POST" }),
    onSuccess: () => setNotice("企业微信测试消息已发送"),
  });
  const testEmail = useMutation({
    mutationFn: () =>
      api("/api/report-settings/test-email", { method: "POST" }),
    onSuccess: () => setNotice("测试提醒邮件已发送"),
  });
  const [endpoint, setEndpoint] = useState({
    llm_base_url: user.preferences.llm_base_url || "",
    model_catalog_url: user.preferences.model_catalog_url || "",
    llm_api_style: user.preferences.llm_api_style || "anthropic",
  });
  const saveEndpoint = useMutation({
    mutationFn: () =>
      api<{
        llm_base_url: string | null;
        model_catalog_url: string | null;
        llm_api_style: string | null;
      }>("/api/users/me/preferences/llm-endpoint", {
        method: "PATCH",
        body: JSON.stringify({
          llm_base_url: endpoint.llm_base_url.trim() || null,
          model_catalog_url: endpoint.model_catalog_url.trim() || null,
          llm_api_style: endpoint.llm_api_style,
        }),
      }),
    onSuccess: async (data) => {
      setEndpoint({
        llm_base_url: data.llm_base_url || "",
        model_catalog_url: data.model_catalog_url || "",
        llm_api_style: data.llm_api_style || "anthropic",
      });
      queryClient.invalidateQueries({ queryKey: ["models"] });
      try {
        onUserChange(await api<User>("/api/users/me"));
      } catch {
        /* 保存成功即可，用户信息刷新失败不阻断 */
      }
      setNotice("模型端点已保存");
    },
  });
  const labels: Record<string, string> = {
    llm_api_key: "4SAPI API Key",
    wecom_webhook_key: "企业微信 Webhook Key",
    qq_smtp_auth_code: "QQ SMTP 授权码",
    wechat_app_id: "公众号 AppID",
    wechat_app_secret: "公众号 AppSecret",
    wecom_aibot_id: "企微智能机器人 Bot ID",
    wecom_aibot_secret: "企微智能机器人 Secret",
  };
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>系统设置</h2>
          <p>密钥只提交到后端加密保存，浏览器不保存明文。</p>
        </div>
      </div>
      <div className="settings-grid">
        <Panel title="模型与账户" meta={user.username}>
          <div className="setting-copy">
            <p>当前默认模型</p>
            <strong>{user.preferences.default_model_id || "尚未设置"}</strong>
            <small>请在任务编排页面搜索并设为默认模型。</small>
          </div>
        </Panel>
        <Panel title="安全说明" meta="后端加密">
          <div className="security-note">
            <LockKeyhole size={20} />
            <p>
              凭据读取接口只返回掩码。日志、SSE、日报和导出包均不得出现明文密钥。
            </p>
          </div>
        </Panel>
      </div>
      <Panel title="外部服务凭据" meta="支持更换与清除">
        <div className="credential-list">
          {Object.keys(labels).map((kind) => {
            const configured = credentials.data?.items.find(
              (item) => item.kind === kind,
            );
            return (
              <div className="credential-row" key={kind}>
                <div>
                  <strong>{labels[kind]}</strong>
                  <small>
                    {configured ? configured.masked_hint : "未配置"}
                  </small>
                </div>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={values[kind]}
                  onChange={(e) =>
                    setValues((items) => ({ ...items, [kind]: e.target.value }))
                  }
                  placeholder="输入新值，保存后清空"
                />
                <Button
                  primary
                  disabled={!values[kind] || (save.isPending && save.variables?.kind === kind)}
                  onClick={() => save.mutate({ kind, value: values[kind] })}
                >
                  {save.isPending && save.variables?.kind === kind ? "保存中…" : "保存"}
                </Button>
                {configured && (
                  <Button danger onClick={() => remove.mutate(kind)} disabled={remove.isPending}>
                    {remove.isPending ? "清除中…" : "清除"}
                  </Button>
                )}
              </div>
            );
          })}
        </div>
        <div className="test-actions">
          <Button onClick={() => testWecom.mutate()} disabled={testWecom.isPending}>
            <Send size={14} />
            {testWecom.isPending ? "发送中…" : "测试企业微信"}
          </Button>
          <Button onClick={() => testEmail.mutate()} disabled={testEmail.isPending}>
            <Mail size={14} />
            {testEmail.isPending ? "发送中…" : "测试QQ邮箱"}
          </Button>
        </div>
        {(save.error || remove.error || testWecom.error || testEmail.error) && (
          <ErrorBox
            error={
              save.error || remove.error || testWecom.error || testEmail.error
            }
          />
        )}
      </Panel>
      <Panel title="模型端点" meta="留空即用系统默认">
        <div className="endpoint-form">
          <p className="endpoint-hint">
            自定义模型服务地址。仅允许 https:// 或本地 http://localhost / http://127.0.0.1，留空则回退系统默认。
          </p>
          <label>
            <span>LLM Base URL</span>
            <input
              value={endpoint.llm_base_url}
              onChange={(event) =>
                setEndpoint((prev) => ({ ...prev, llm_base_url: event.target.value }))
              }
              placeholder="https://4sapi.org/v1"
            />
          </label>
          <label>
            <span>模型目录 URL</span>
            <input
              value={endpoint.model_catalog_url}
              onChange={(event) =>
                setEndpoint((prev) => ({ ...prev, model_catalog_url: event.target.value }))
              }
              placeholder="https://4sapi.org/v1/models"
            />
          </label>
          <label>
            <span>接口风格</span>
            <select
              value={endpoint.llm_api_style}
              onChange={(event) =>
                setEndpoint((prev) => ({ ...prev, llm_api_style: event.target.value }))
              }
            >
              <option value="anthropic">Anthropic 原生 /v1/messages</option>
              <option value="openai">OpenAI 兼容 /v1/chat/completions</option>
            </select>
          </label>
          <div className="actions">
            <Button
              primary
              onClick={() => saveEndpoint.mutate()}
              disabled={saveEndpoint.isPending}
            >
              <Save size={14} />
              {saveEndpoint.isPending ? "保存中…" : "保存端点"}
            </Button>
          </div>
          {saveEndpoint.error && <ErrorBox error={saveEndpoint.error} />}
        </div>
      </Panel>
    </div>
  );
}

type QaCitation = { source?: string; ref?: string; note?: string };
type QaMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  citations: QaCitation[];
  model_id?: string | null;
  grounded: boolean;
  created_at: string;
};
type QaConversationSummary = {
  id: string;
  title: string;
  source?: string;
  created_at: string;
  updated_at: string;
};
type QaConversationDetail = QaConversationSummary & { messages: QaMessage[] };

function QaMessageBubble({ message }: { message: QaMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={`qa-msg ${isUser ? "qa-msg-user" : "qa-msg-assistant"}`}>
      <div className="qa-bubble">
        {!isUser && !message.grounded && (
          <span className="qa-badge-fallback">
            <AlertTriangle size={11} aria-hidden="true" />
            非本库来源 · 仅供参考
          </span>
        )}
        <p className="qa-bubble-text">{message.content}</p>
        {!isUser && message.citations.length > 0 && (
          <details className="qa-citations">
            <summary>引用来源（{message.citations.length}）</summary>
            <ul>
              {message.citations.map((cite, index) => (
                <li key={`${cite.ref ?? cite.source ?? index}-${index}`}>
                  <strong>{cite.source || "来源"}</strong>
                  {cite.ref && <code>{cite.ref}</code>}
                  {cite.note && <span>{cite.note}</span>}
                </li>
              ))}
            </ul>
          </details>
        )}
        {!isUser && message.model_id && (
          <small className="qa-model-tag">{message.model_id}</small>
        )}
      </div>
    </div>
  );
}

type QaStage = {
  key: string;
  label: string;
  status: "started" | "done" | "error";
  detail?: string;
  group?: string;
};

// 收到 stage 事件时按 key 覆盖更新（started→done 原地改状态），首次出现则追加。
function upsertStage(prev: QaStage[], event: Record<string, unknown>): QaStage[] {
  const key = String(event.key ?? "");
  const next: QaStage = {
    key,
    label: String(event.label ?? ""),
    status: (event.status as QaStage["status"]) ?? "started",
    detail: event.detail ? String(event.detail) : undefined,
    group: event.group ? String(event.group) : undefined,
  };
  const index = prev.findIndex((stage) => stage.key === key);
  if (index === -1) return [...prev, next];
  const copy = prev.slice();
  copy[index] = { ...copy[index], ...next };
  return copy;
}

function QaStageTracker({ stages }: { stages: QaStage[] }) {
  return (
    <ol className="qa-steps">
      {stages.map((stage) => (
        <li key={stage.key} className={`qa-step qa-step-${stage.status}${stage.group === "step" ? " qa-step-sub" : ""}`}>
          <span className="qa-step-icon" aria-hidden="true">
            {stage.status === "done" ? (
              <CheckCircle2 size={13} />
            ) : stage.status === "error" ? (
              <AlertCircle size={13} />
            ) : (
              <RefreshCw className="spin" size={13} />
            )}
          </span>
          <span className="qa-step-label">{stage.label}</span>
          {stage.detail && <span className="qa-step-detail">{stage.detail}</span>}
        </li>
      ))}
    </ol>
  );
}

function QaPage({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [model, setModel] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [stages, setStages] = useState<QaStage[]>([]);
  const [streamAnswer, setStreamAnswer] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const streamRef = useRef<HTMLDivElement>(null);
  const models = useQuery<{ items: Array<{ id: string }>; default_model_id?: string }>({
    queryKey: ["models"],
    queryFn: () => api("/api/models?refresh=true"),
    retry: false,
  });
  const conversations = useQuery<{ items: QaConversationSummary[] }>({
    queryKey: ["qa-conversations"],
    queryFn: () => api("/api/qa/conversations"),
  });
  const active = useQuery<QaConversationDetail>({
    queryKey: ["qa-conversation", activeId],
    queryFn: () => api(`/api/qa/conversations/${activeId}`),
    enabled: !!activeId,
  });
  useEffect(() => {
    if (!model) {
      setModel(user.preferences.default_model_id || models.data?.default_model_id || "");
    }
  }, [user.preferences.default_model_id, models.data?.default_model_id, model]);
  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight });
  }, [active.data?.messages?.length, pending, stages, streamAnswer]);
  const createConversation = useMutation({
    mutationFn: () =>
      api<QaConversationSummary>("/api/qa/conversations", {
        method: "POST",
        body: JSON.stringify({ title: "新会话" }),
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["qa-conversations"] });
      setActiveId(data.id);
    },
  });
  const runAsk = async (conversationId: string, text: string) => {
    let failed: string | null = null;
    await streamNdjson(
      `/api/qa/conversations/${conversationId}/ask/stream`,
      { question: text, model_id: model || null },
      (event) => {
        const type = event.type as string;
        if (type === "stage") {
          setStages((prev) => upsertStage(prev, event));
        } else if (type === "token") {
          setStreamAnswer((prev) => prev + String(event.text ?? ""));
        } else if (type === "error") {
          failed = String(event.detail ?? "作答失败");
        }
      },
    );
    if (failed) throw new ApiError(502, failed);
    queryClient.invalidateQueries({ queryKey: ["qa-conversation", conversationId] });
    queryClient.invalidateQueries({ queryKey: ["qa-conversations"] });
  };
  const removeConversation = useMutation({
    mutationFn: (id: string) => api(`/api/qa/conversations/${id}`, { method: "DELETE" }),
    onSuccess: (_, id) => {
      if (activeId === id) setActiveId(null);
      queryClient.invalidateQueries({ queryKey: ["qa-conversations"] });
      setNotice("会话已删除");
    },
  });
  const send = async () => {
    const text = question.trim();
    if (!text || streaming) return;
    setQuestion("");
    setPending(text);
    setStages([]);
    setStreamAnswer("");
    setStreamError(null);
    setStreaming(true);
    try {
      let conversationId = activeId;
      if (!conversationId) {
        conversationId = (await createConversation.mutateAsync()).id;
      }
      await runAsk(conversationId, text);
      setPending(null);
      setStages([]);
      setStreamAnswer("");
    } catch (err) {
      setStreamError(err instanceof ApiError ? err.message : "作答失败，请重试");
      setPending(null);
      setQuestion(text);
    } finally {
      setStreaming(false);
    }
  };
  const items = conversations.data?.items || [];
  const messages = active.data?.messages || [];
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>智能问答</h2>
          <p>基于经营模型、仿真引擎与项目知识库综合作答，答案附可追溯来源。</p>
        </div>
        <Button primary onClick={() => setActiveId(null)}>
          <Sparkles size={15} />
          新建会话
        </Button>
      </div>
      <div className="qa-layout">
        <Panel title="历史会话" meta={`${items.length} 个`} className="qa-list-panel">
          {conversations.isLoading ? (
            <Loading />
          ) : items.length === 0 ? (
            <div className="reference-empty">还没有会话，直接提问即可开启。</div>
          ) : (
            <ul className="qa-conv-list">
              {items.map((conv) => (
                <li
                  key={conv.id}
                  className={conv.id === activeId ? "qa-conv-item active" : "qa-conv-item"}
                >
                  <button type="button" onClick={() => setActiveId(conv.id)}>
                    {conv.source === "wecom" && (
                      <span className="qa-conv-badge" title="来自企业微信群 @机器人">群</span>
                    )}
                    <span>{conv.title}</span>
                  </button>
                  <button
                    type="button"
                    className="qa-conv-del"
                    aria-label="删除会话"
                    title="删除该会话"
                    onClick={() => {
                      if (window.confirm(`删除会话「${conv.title}」及其全部消息？此操作不可恢复。`)) {
                        removeConversation.mutate(conv.id);
                      }
                    }}
                    disabled={removeConversation.isPending}
                  >
                    <Trash2 size={13} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel
          title={active.data?.title || "新会话"}
          meta="严格接地 · 通用兜底"
          className="qa-chat-panel"
        >
          <div className="qa-stream" ref={streamRef}>
            {activeId && active.isLoading ? (
              <Loading />
            ) : messages.length === 0 && !pending ? (
              <div className="reference-empty">
                问我成本预测、良率影响、场景对比等问题，我会结合模型与知识库作答。
              </div>
            ) : (
              messages.map((message) => (
                <QaMessageBubble key={message.id} message={message} />
              ))
            )}
            {pending && (
              <>
                <div className="qa-msg qa-msg-user">
                  <div className="qa-bubble">
                    <p className="qa-bubble-text">{pending}</p>
                  </div>
                </div>
                <div className="qa-msg qa-msg-assistant">
                  <div className="qa-bubble qa-bubble-live">
                    {stages.length > 0 && <QaStageTracker stages={stages} />}
                    {streamAnswer ? (
                      <p className="qa-bubble-text">
                        {streamAnswer}
                        {streaming && <span className="qa-caret" />}
                      </p>
                    ) : (
                      stages.length === 0 && (
                        <span className="qa-bubble-thinking">
                          <RefreshCw className="spin" size={13} />
                          正在综合模型与知识库…
                        </span>
                      )
                    )}
                  </div>
                </div>
              </>
            )}
          </div>
          {streamError && <ErrorBox error={new ApiError(502, streamError)} />}
          <div className="qa-composer">
            <select value={model} onChange={(event) => setModel(event.target.value)}>
              {model && !(models.data?.items || []).some((item) => item.id === model) && (
                <option value={model}>{model}</option>
              )}
              {(models.data?.items || []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.id}
                </option>
              ))}
            </select>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                  event.preventDefault();
                  send();
                }
              }}
              placeholder="例如：良率提升 2% 对单月利润的影响，Ctrl+Enter 发送"
              rows={2}
            />
            <Button primary onClick={send} disabled={!question.trim() || streaming}>
              <Send size={15} />
              {streaming ? "作答中…" : "发送"}
            </Button>
          </div>
        </Panel>
      </div>
    </div>
  );
}

export default function App() {
  const [statusData, setStatusData] = useState<{
    setup_required: boolean;
  } | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);
  useEffect(() => {
    api<{ setup_required: boolean }>("/api/auth/status")
      .then(async (status) => {
        setStatusData(status);
        if (!status.setup_required) {
          try {
            setUser(await api<User>("/api/users/me"));
          } catch {
            /* login required */
          }
        }
      })
      .finally(() => setChecked(true));
  }, []);
  const logout = async () => {
    await api("/api/auth/logout", { method: "POST" });
    setUser(null);
    setStatusData({ setup_required: false });
  };
  if (!checked) return <Loading text="正在连接控制台…" />;
  if (!user)
    return (
      <AuthScreen
        setupRequired={Boolean(statusData?.setup_required)}
        onAuthenticated={setUser}
      />
    );
  return <Layout user={user} onLogout={logout} onUserChange={setUser} />;
}
