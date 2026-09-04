import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Handle,
  Position,
  MarkerType,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import * as dagre from "@dagrejs/dagre";
import { Boxes, Share2, Search, RefreshCw } from "lucide-react";
import "@xyflow/react/dist/style.css";
import { api } from "./api";

type ClassNodeD = { id: string; label: string; module: string; subClassCount: number };
type ClassEdgeD = { source: string; target: string; kind: "subClassOf" | "relation"; label: string };
type InstNodeD = { id: string; label: string; typeIri: string; typeLabel: string };
type InstEdgeD = { source: string; target: string; label: string };
type GraphData = {
  classes: { nodes: ClassNodeD[]; edges: ClassEdgeD[] };
  instances: { nodes: InstNodeD[]; edges: InstEdgeD[] };
  modules: string[];
};

const PALETTE = ["#2864dc", "#0e9aa7", "#7c58d6", "#d97706", "#0f9d58", "#d14d6b", "#1f8fd6", "#b8558a", "#4c7a1f", "#c2410c"];
function moduleColor(mod: string): string {
  let h = 0;
  for (const ch of mod) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
function typeColor(key: string): string {
  let h = 0;
  for (const ch of key) h = (h * 33 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360} 62% 55%)`;
}
const CLASS_W = 192, CLASS_H = 58, INST_W = 200, INST_H = 62;

// ---- 自定义节点：带渐变描边、入场弹出与悬停浮起的动态效果 ----
function ClassNodeBox({ data }: NodeProps) {
  const d = data as { label: string; module: string; count: number; accent: string; faded: boolean };
  return (
    <div
      className={`og-node og-class${d.faded ? " og-faded" : ""}`}
      style={{ ["--accent" as string]: d.accent }}
    >
      <Handle type="target" position={Position.Top} className="og-handle" />
      <span className="og-node-kind">类 · {d.module}</span>
      <strong className="og-node-label" title={d.label}>{d.label}</strong>
      {d.count > 0 && <span className="og-node-badge">{d.count} 子类</span>}
      <Handle type="source" position={Position.Bottom} className="og-handle" />
    </div>
  );
}

function InstNodeBox({ data }: NodeProps) {
  const d = data as { label: string; typeLabel: string; accent: string; seed: boolean };
  return (
    <div
      className={`og-node og-inst${d.seed ? " og-seed" : " og-neighbor"}`}
      style={{ ["--accent" as string]: d.accent }}
    >
      <Handle type="target" position={Position.Left} className="og-handle" />
      <span className="og-node-kind" style={{ color: d.accent }}>◆ {d.typeLabel}</span>
      <strong className="og-node-label" title={d.label}>{d.label}</strong>
      <Handle type="source" position={Position.Right} className="og-handle" />
    </div>
  );
}

const nodeTypes = { ogClass: ClassNodeBox, ogInst: InstNodeBox };

// ---- dagre 自动布局：把类树 / 实例网络（含众多小连通块）整齐排布 ----
function layout(
  nodes: Node[],
  edges: Edge[],
  dir: "TB" | "LR",
  w: number,
  h: number,
): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: dir, nodesep: dir === "TB" ? 34 : 26, ranksep: dir === "TB" ? 66 : 96, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: w, height: h });
  for (const e of edges) if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  dagre.layout(g);
  return nodes.map((n) => {
    const p = g.node(n.id);
    return { ...n, position: { x: p.x - w / 2, y: p.y - h / 2 } };
  });
}

// 把「众多小连通块」拆分、各自 dagre 排布，再网格拼装成画廊，避免单列堆叠导致缩到看不清
function layoutClusters(nodes: Node[], edges: Edge[], w: number, h: number): Node[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const adj = new Map<string, Set<string>>();
  for (const n of nodes) adj.set(n.id, new Set());
  for (const e of edges) {
    adj.get(e.source)?.add(e.target);
    adj.get(e.target)?.add(e.source);
  }
  // 连通块
  const seen = new Set<string>();
  const comps: string[][] = [];
  for (const n of nodes) {
    if (seen.has(n.id)) continue;
    const stack = [n.id], comp: string[] = [];
    while (stack.length) {
      const x = stack.pop()!;
      if (seen.has(x)) continue;
      seen.add(x); comp.push(x);
      for (const nb of adj.get(x) ?? []) if (!seen.has(nb)) stack.push(nb);
    }
    comps.push(comp);
  }
  // 各连通块独立 dagre，记录相对坐标与包围盒
  const laid = comps.map((comp) => {
    const cset = new Set(comp);
    const cnodes = comp.map((id) => byId.get(id)!);
    const cedges = edges.filter((e) => cset.has(e.source) && cset.has(e.target));
    const positioned = layout(cnodes, cedges, "LR", w, h);
    const xs = positioned.map((p) => p.position.x);
    const ys = positioned.map((p) => p.position.y);
    const minX = Math.min(...xs), minY = Math.min(...ys);
    const width = Math.max(...xs) - minX + w, height = Math.max(...ys) - minY + h;
    return { positioned, minX, minY, width, height };
  });
  // 高的簇优先，网格行排布（每行按宽度累加，超阈值换行）
  laid.sort((a, b) => b.height - a.height);
  const GAP = 46;
  const maxRowW = Math.max(1600, Math.sqrt(laid.reduce((s, c) => s + c.width * c.height, 0)) * 1.6);
  const out: Node[] = [];
  let cx = 0, cy = 0, rowH = 0;
  for (const c of laid) {
    if (cx > 0 && cx + c.width > maxRowW) { cx = 0; cy += rowH + GAP; rowH = 0; }
    for (const p of c.positioned) {
      out.push({ ...p, position: { x: p.position.x - c.minX + cx, y: p.position.y - c.minY + cy } });
    }
    cx += c.width + GAP;
    rowH = Math.max(rowH, c.height);
  }
  return out;
}

const CURATED = "__curated__"; // 全部精选（排除自动生成的 generated 模块）

// ---- 类拓扑：按模块过滤，保留边两端可见的 subClassOf / 关系，外部邻居淡显 ----
function buildClassGraph(data: GraphData, module: string): { nodes: Node[]; edges: Edge[] } {
  const inScope = (m: string) =>
    module === CURATED ? m !== "generated" : m === module;
  const primary = new Set(data.classes.nodes.filter((n) => inScope(n.module)).map((n) => n.id));
  const modOf = new Map(data.classes.nodes.map((n) => [n.id, n.module]));
  // subClassOf 父邻接（child -> parents），用于把层级向上补全到根
  const parentsOf = new Map<string, string[]>();
  for (const e of data.classes.edges) {
    if (e.kind !== "subClassOf") continue;
    (parentsOf.get(e.source) ?? parentsOf.set(e.source, []).get(e.source)!).push(e.target);
  }
  // 单模块视图：补全每个类的 subClassOf 父链（可跨模块，淡显），让层级有根，
  // 但不引入 domain/range 的 range 邻居——那会把 generated 模块的大量类拉进来。
  const visible = new Set(primary);
  if (module !== CURATED) {
    const stack = [...primary];
    while (stack.length) {
      const cur = stack.pop()!;
      for (const p of parentsOf.get(cur) ?? []) {
        if (!visible.has(p)) { visible.add(p); stack.push(p); }
      }
    }
  }
  const labelOf = new Map(data.classes.nodes.map((n) => [n.id, n.label]));
  const countOf = new Map(data.classes.nodes.map((n) => [n.id, n.subClassCount]));
  const nodes: Node[] = [...visible].map((id) => {
    const mod = modOf.get(id) ?? "external";
    return {
      id,
      type: "ogClass",
      position: { x: 0, y: 0 },
      data: {
        label: labelOf.get(id) ?? id,
        module: mod,
        count: countOf.get(id) ?? 0,
        accent: moduleColor(mod),
        faded: !primary.has(id),
      },
    };
  });
  const edges: Edge[] = [];
  const seen = new Set<string>();
  for (const e of data.classes.edges) {
    if (!visible.has(e.source) || !visible.has(e.target)) continue;
    // 关系线仅在选中范围内的类之间展示，避免连向淡显父类造成噪声
    if (e.kind === "relation" && (!primary.has(e.source) || !primary.has(e.target))) continue;
    const id = `${e.kind}:${e.source}->${e.target}:${e.label}`;
    if (seen.has(id)) continue;
    seen.add(id);
    const sub = e.kind === "subClassOf";
    edges.push({
      id,
      source: sub ? e.target : e.source, // subClassOf 反向连线，父类在上
      target: sub ? e.source : e.target,
      label: sub ? undefined : e.label,
      type: "default",
      className: sub ? "og-edge-sub" : "og-edge-rel",
      animated: sub,
      markerEnd: sub ? undefined : { type: MarkerType.ArrowClosed, color: "#7c58d6", width: 14, height: 14 },
    });
  }
  return { nodes: layout(nodes, edges, "TB", CLASS_W, CLASS_H), edges };
}

// ---- 实例拓扑：类型过滤 + 关键字 + 仅显示有关联个体，按类型着色 ----
function buildInstanceGraph(
  data: GraphData,
  typeLabel: string,
  search: string,
  connectedOnly: boolean,
): { nodes: Node[]; edges: Edge[] } {
  const deg = new Map<string, number>();
  for (const e of data.instances.edges) {
    deg.set(e.source, (deg.get(e.source) ?? 0) + 1);
    deg.set(e.target, (deg.get(e.target) ?? 0) + 1);
  }
  const kw = search.trim().toLowerCase();
  // 种子选取：有关键字时按关键字跨全部类型检索（更符合直觉）；否则按选中类型。
  const seeds = data.instances.nodes.filter((n) => {
    if (kw) return n.label.toLowerCase().includes(kw) || n.id.toLowerCase().includes(kw);
    if (typeLabel !== "__all__" && typeLabel !== "" && n.typeLabel !== typeLabel) return false;
    if (connectedOnly && !(deg.get(n.id) ?? 0)) return false;
    return true;
  });
  const seedIds = new Set(seeds.map((n) => n.id));
  // 邻居扩展：把每个种子的一跳邻居（任意类型）纳入，还原完整诊断簇
  const adj = new Map<string, Set<string>>();
  for (const e of data.instances.edges) {
    (adj.get(e.source) ?? adj.set(e.source, new Set()).get(e.source)!).add(e.target);
    (adj.get(e.target) ?? adj.set(e.target, new Set()).get(e.target)!).add(e.source);
  }
  const keepIds = new Set(seedIds);
  if (kw || (typeLabel !== "__all__" && typeLabel !== "")) {
    for (const s of seedIds) for (const nb of adj.get(s) ?? []) keepIds.add(nb);
  }
  const nodeOf = new Map(data.instances.nodes.map((n) => [n.id, n]));
  const nodes: Node[] = [...keepIds].map((id) => {
    const n = nodeOf.get(id)!;
    return {
      id,
      type: "ogInst",
      position: { x: 0, y: 0 },
      data: {
        label: n.label,
        typeLabel: n.typeLabel || "未归类",
        accent: typeColor(n.typeLabel || n.typeIri),
        seed: seedIds.has(id),
      },
    };
  });
  const edges: Edge[] = [];
  for (const e of data.instances.edges) {
    if (!keepIds.has(e.source) || !keepIds.has(e.target)) continue;
    edges.push({
      id: `${e.source}->${e.target}:${e.label}`,
      source: e.source,
      target: e.target,
      label: e.label,
      type: "default",
      className: "og-edge-inst",
      markerEnd: { type: MarkerType.ArrowClosed, color: "#0e9aa7", width: 14, height: 14 },
    });
  }
  return { nodes: layoutClusters(nodes, edges, INST_W, INST_H), edges };
}

export default function OntologyGraph() {
  const { data, isLoading, error } = useQuery<GraphData>({
    queryKey: ["ontology-graph"],
    queryFn: () => api<GraphData>("/api/ontology/graph"),
    staleTime: 60_000,
  });

  const [view, setView] = useState<"class" | "instance">("class");
  const [module, setModule] = useState<string>("equipment");
  const [typeLabel, setTypeLabel] = useState<string>("");
  const [search, setSearch] = useState("");
  const [connectedOnly, setConnectedOnly] = useState(true);

  const instanceTypes = useMemo(() => {
    if (!data) return [] as { label: string; count: number }[];
    const c = new Map<string, number>();
    for (const n of data.instances.nodes) {
      const k = n.typeLabel || "未归类";
      c.set(k, (c.get(k) ?? 0) + 1);
    }
    return [...c.entries()].map(([label, count]) => ({ label, count })).sort((a, b) => b.count - a.count);
  }, [data]);

  // 首次载入实例数据时，选一个能形成清晰诊断簇的种子类型（优先 Playbook/手册这类枢纽）
  useEffect(() => {
    if (typeLabel || instanceTypes.length === 0) return;
    const hub = instanceTypes.find((t) => /playbook|手册/i.test(t.label));
    setTypeLabel(hub?.label ?? instanceTypes[0].label);
  }, [instanceTypes, typeLabel]);

  const graph = useMemo(() => {
    if (!data) return { nodes: [] as Node[], edges: [] as Edge[] };
    return view === "class"
      ? buildClassGraph(data, module)
      : buildInstanceGraph(data, typeLabel, search, connectedOnly);
  }, [data, view, module, typeLabel, search, connectedOnly]);

  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const rf = useRef<ReactFlowInstance<Node, Edge> | null>(null);

  useEffect(() => {
    setRfNodes(graph.nodes);
    setRfEdges(graph.edges);
    const t = setTimeout(() => rf.current?.fitView({ padding: 0.18, duration: 400 }), 60);
    return () => clearTimeout(t);
  }, [graph, setRfNodes, setRfEdges]);

  const animate = graph.edges.length <= 220;

  return (
    <div className="page og-page">
      <div className="page-head">
        <div>
          <h2>本体关系图</h2>
          <p>类拓扑与实例拓扑的可视化探索 · 仅读取现有本体，不触发任何写入或推理提升</p>
        </div>
      </div>

      <div className="og-toolbar">
        <div className="og-seg">
          <button className={view === "class" ? "active" : ""} onClick={() => setView("class")}>
            <Boxes size={14} /> 类拓扑
          </button>
          <button className={view === "instance" ? "active" : ""} onClick={() => setView("instance")}>
            <Share2 size={14} /> 实例拓扑
          </button>
        </div>

        {view === "class" ? (
          <label className="og-field">
            模块
            <select value={module} onChange={(e) => setModule(e.target.value)}>
              <option value={CURATED}>全部精选（不含 generated）</option>
              {data?.modules.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </label>
        ) : (
          <>
            <label className="og-field">
              类型
              <select value={typeLabel} onChange={(e) => setTypeLabel(e.target.value)}>
                <option value="__all__">全部类型</option>
                {instanceTypes.map((t) => (
                  <option key={t.label} value={t.label}>{t.label}（{t.count}）</option>
                ))}
              </select>
            </label>
            <label className="og-field og-search">
              <Search size={13} />
              <input placeholder="搜索个体标签或 IRI" value={search} onChange={(e) => setSearch(e.target.value)} />
            </label>
            <label className="og-check">
              <input type="checkbox" checked={connectedOnly} onChange={(e) => setConnectedOnly(e.target.checked)} />
              仅显示有关联个体
            </label>
          </>
        )}

        <span className="og-count">
          {graph.nodes.length} 节点 · {graph.edges.length} 关系
        </span>
      </div>

      <section className="panel og-canvas-panel">
        {isLoading ? (
          <div className="loading"><RefreshCw className="spin" size={18} /> 正在加载本体关系数据…</div>
        ) : error ? (
          <div className="error-box">{error instanceof Error ? error.message : String(error)}</div>
        ) : graph.nodes.length === 0 ? (
          <div className="empty">当前筛选条件下没有可展示的节点</div>
        ) : (
          <div className="og-canvas">
            <ReactFlow
              nodes={rfNodes}
              edges={rfEdges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onInit={(inst) => { rf.current = inst; }}
              defaultEdgeOptions={{ animated: animate }}
              minZoom={0.15}
              maxZoom={1.8}
              proOptions={{ hideAttribution: true }}
              fitView
              fitViewOptions={{ padding: 0.18 }}
            >
              <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#d5deea" />
              <Controls showInteractive={false} />
              <MiniMap
                pannable
                zoomable
                nodeColor={(n) => (n.data as { accent?: string })?.accent ?? "#2864dc"}
                nodeStrokeWidth={2}
                maskColor="rgba(8,27,51,.06)"
              />
              <div className="og-legend">
                {view === "class" ? (
                  <>
                    <span><i className="lg-sub" /> subClassOf（父类在上）</span>
                    <span><i className="lg-rel" /> 对象属性 domain→range</span>
                  </>
                ) : (
                  <span><i className="lg-inst" /> 个体间对象属性断言</span>
                )}
              </div>
            </ReactFlow>
          </div>
        )}
      </section>
    </div>
  );
}
