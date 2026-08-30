import { useEffect, useMemo, useState } from "react";
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
  ShieldCheck,
  Sparkles,
  Workflow,
} from "lucide-react";
import {
  api,
  type AgentConfig,
  type RunInfo,
  type RunReference,
  type SourceMode,
} from "./api";
import { useAppStore } from "./store";

type User = {
  id: number;
  username: string;
  preferences: {
    default_model_id?: string;
    default_agent_count: number;
    timezone: string;
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

const nav = [
  ["/", "总览", LayoutDashboard],
  ["/orchestrator", "任务编排", Bot],
  ["/ontology", "本体中心", Network],
  ["/knowledge", "知识库", Database],
  ["/business", "经营模型", Gauge],
  ["/simulation", "仿真引擎", Cpu],
  ["/scenarios", "业务场景", FileText],
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
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="/business" element={<Business />} />
          <Route path="/simulation" element={<Simulation />} />
          <Route path="/scenarios" element={<Scenarios />} />
          <Route path="/reports" element={<Reports user={user} />} />
          <Route path="/exports" element={<ExportCenter />} />
          <Route path="/history" element={<RunHistory />} />
          <Route path="/settings" element={<SettingsPage user={user} />} />
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
}: {
  label: string;
  value: number | string;
  delta?: number;
}) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong>
        {typeof value === "number" ? value.toLocaleString() : value}
      </strong>
      <small>
        {delta !== undefined
          ? `今日 +${delta.toLocaleString()}`
          : "当前正式总量"}
      </small>
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
  owl_shacl_reasoning: "OWL / SHACL",
  business_simulation: "经营仿真",
  scenario_article: "场景沉淀",
  finalize_round: "轮次归档",
  between_rounds: "轮次间隔",
  recovering: "断点恢复",
  round_failed: "轮次失败",
  completed: "完成",
};

function RunStatusIcon({ status }: { status?: string }) {
  if (status === "completed") return <CheckCircle2 size={19} />;
  if (status === "failed" || status === "round_failed")
    return <AlertTriangle size={19} />;
  if (status === "paused") return <PauseCircle size={19} />;
  if (status === "needs_attention") return <AlertCircle size={19} />;
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
    refetchInterval: 3000,
  });
  const [events, setEvents] = useState<
    Array<{ message: string; level: string; created_at: string }>
  >([]);
  const latest = data?.latest_run;
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
    mutationFn: ({ name }: { name: string }) =>
      api(`/api/runs/${latest?.id}/${name}`, { method: "POST" }),
    onSuccess: (_result, variables) => {
      setNotice(variables.name === "pause" ? "任务已暂停" : variables.name === "resume" ? "任务已恢复" : variables.name === "cancel" ? "任务已停止" : "已提交轮次停止请求");
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
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
  const d = data.metrics.today_added;
  const metrics = [
    ["OWL 类", t.classes ?? 0, d.classes],
    ["属性", t.properties ?? 0, d.properties],
    ["关系", t.relations ?? 0, d.relations],
    ["实例", t.individuals ?? 0, d.individuals],
    ["知识条目", t.knowledge_entries ?? 0, d.knowledge_entries],
    ["问题域覆盖率", `${t.coverage_percent ?? 0}%`, undefined],
  ] as Array<[string, number | string, number | undefined]>;
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
                    ? `${latest.status === "completed" ? "持续协作任务已完成" : latest.status === "failed" ? "持续协作任务失败" : latest.status === "needs_attention" ? "持续协作任务需处理" : latest.status === "paused" ? "持续协作任务已暂停" : `持续协作任务${latest.status === "between_rounds" ? "等待下一轮" : "运行中"}`} · 第 ${latest.current_round || 1} 轮`
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
              "recovering",
              "paused",
              "between_rounds",
              "stopping_after_round",
              "needs_attention",
            ].includes(latest.status) && (
              <>
                <Button
                  disabled={action.isPending}
                  onClick={() =>
                    action.mutate({
                      name:
                        latest.status === "paused" ||
                        latest.status === "needs_attention"
                          ? "resume"
                          : "pause",
                    })
                  }
                >
                  {latest.status === "paused" ||
                  latest.status === "needs_attention" ? (
                    <Play size={15} />
                  ) : (
                    <Pause size={15} />
                  )}
                  {latest.status === "paused" ||
                  latest.status === "needs_attention"
                    ? "恢复"
                    : "暂停"}
                </Button>
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
      <div className="metrics-grid">
        {metrics.map(([label, value, delta]) => (
          <MetricCard key={label} label={label} value={value} delta={delta} />
        ))}
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
                    {currentIndex > index || latest?.status === "completed"
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
  const loop = useQuery<{ enabled: boolean; interval_minutes: number }>({
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
    max_consecutive_round_failures: 3,
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
          Schema、来源、内部特征/vFab、能力问题、OWL、SHACL、推理、经营模型和仿真全部通过后执行；失败候选不会写入正式库。
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
  const t = metrics.data!.totals;
  const d = metrics.data!.today_added;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>本体中心</h2>
          <p>标准OWL语义、SHACL约束和可执行推理规则。</p>
        </div>
        <ExportButton kind="ontology" />
      </div>
      <div className="metrics-grid six">
        <MetricCard label="类 Class" value={t.classes || 0} delta={d.classes} />
        <MetricCard
          label="属性 Property"
          value={t.properties || 0}
          delta={d.properties}
        />
        <MetricCard
          label="关系 Relation"
          value={t.relations || 0}
          delta={d.relations}
        />
        <MetricCard
          label="实例 Individual"
          value={t.individuals || 0}
          delta={d.individuals}
        />
        <MetricCard label="公理 Axiom" value={t.axioms || 0} delta={d.axioms} />
        <MetricCard label="推理规则" value={t.rules || 0} delta={d.rules} />
      </div>
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
      query={query}
    />
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
      query={query}
    />
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
      query={query}
    />
  );
}

function CatalogPage({
  title,
  subtitle,
  icon,
  exportKind,
  query,
}: {
  title: string;
  subtitle: string;
  icon: React.ReactNode;
  exportKind: string;
  query: {
    isLoading: boolean;
    error: unknown;
    data?: { items: Array<{ path: string; name: string; document: unknown }> };
  };
}) {
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <ExportButton kind={exportKind} />
      </div>
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

function RunHistory() {
  const [selected, setSelected] = useState<string | null>(null);
  const setNotice = useAppStore((state) => state.setNotice);
  const query = useQuery<{ items: RunInfo[] }>({
    queryKey: ["runs"],
    queryFn: () => api("/api/runs"),
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
      artifacts: { evidence_pages?: number; scenario_articles?: number };
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
      </div>
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
                      <Button onClick={() => retry.mutate(run.id)} disabled={retry.isPending}>
                        {retry.isPending ? "启动中…" : "重新启动"}
                      </Button>
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

function SettingsPage({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const setNotice = useAppStore((state) => state.setNotice);
  const [values, setValues] = useState<Record<string, string>>({
    llm_api_key: "",
    wecom_webhook_key: "",
    qq_smtp_auth_code: "",
    wechat_app_id: "",
    wechat_app_secret: "",
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
  const labels: Record<string, string> = {
    llm_api_key: "4SAPI API Key",
    wecom_webhook_key: "企业微信 Webhook Key",
    qq_smtp_auth_code: "QQ SMTP 授权码",
    wechat_app_id: "公众号 AppID",
    wechat_app_secret: "公众号 AppSecret",
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
