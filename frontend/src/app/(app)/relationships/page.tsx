"use client";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/auth";
import { Badge, Button, Card, Field, Input, Modal, PageHeader, Select, Spinner, Table, Tabs, Toggle, statusTone } from "@/components/ui";
import { Check, Plus, Sparkles, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";

const ReactECharts = dynamic(() => import("echarts-for-react"), { ssr: false });

type Rel = { id: string; from_column_id: string; to_column_id: string; from_table: string; from_column: string; from_source: string; to_table: string; to_column: string; to_source: string; type: string; confidence: number; reason?: string | null; status: string; is_cross_source: boolean; created_by: string; evidence: Record<string, unknown> };
type Graph = { nodes: { id: string; label: string; domain?: string | null; source?: string | null; rows?: number | null; metrics: string[] }[]; links: { source: string; target: string; label: string; kind: string; confidence: number }[] };
const DOMAIN_COLORS: Record<string, string> = { finance: "#2563eb", sales: "#7c3aed", hr: "#059669", customer: "#0891b2", support: "#d97706", inventory: "#dc2626", operations: "#db2777", marketing: "#65a30d", product: "#f59e0b", procurement: "#6366f1" };

export default function RelationshipsPage() {
  const { user } = useAuth();
  const { dark } = useTheme();
  const [rels, setRels] = useState<Rel[] | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [tab, setTab] = useState("graph");
  const [suggested, setSuggested] = useState(true);
  const [filter, setFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const canEdit = user?.role !== "viewer";
  const load = useCallback(async () => {
    setRels(await api<Rel[]>("/semantic/relationships"));
    setGraph(await api<Graph>(`/semantic/graph?include_suggested=${suggested}`));
  }, [suggested]);
  useEffect(() => {
    load();
  }, [load]);

  const option = useMemo<EChartsOption | null>(() => {
    if (!graph) return null;
    const q = filter.toLowerCase();
    const nodes = graph.nodes.map((n) => ({ id: n.id, name: n.label, symbolSize: Math.min(70, 22 + Math.log10((n.rows || 1) + 1) * 8), value: n.rows || 0, category: n.domain || "other", itemStyle: { color: DOMAIN_COLORS[n.domain || ""] || "#64748b", opacity: q && !n.label.toLowerCase().includes(q) && !(n.domain || "").includes(q) ? 0.15 : 1 }, label: { show: true, color: dark ? "#e6ebf5" : "#0f172a", fontSize: 11 }, tooltip: { formatter: `<b>${n.label}</b><br/>${n.source} · ${n.domain || "—"}<br/>${(n.rows || 0).toLocaleString()} rows<br/>metrics: ${n.metrics.join(", ") || "—"}` } }));
    const cats = Array.from(new Set(nodes.map((n) => n.category))).map((c) => ({ name: c }));
    return {
      tooltip: {},
      legend: [{ data: cats.map((c) => c.name), top: 0, textStyle: { color: dark ? "#93a0bd" : "#64748b", fontSize: 11 } }],
      series: [{ type: "graph", layout: "force", roam: true, draggable: true, data: nodes, categories: cats, edges: graph.links.map((l) => ({ source: l.source, target: l.target, value: l.confidence, lineStyle: { width: 1 + l.confidence * 2, color: l.kind === "entity" ? "#a78bfa" : dark ? "#4b5a80" : "#94a3b8", type: l.kind === "entity" ? "dashed" : "solid", curveness: 0.1 }, label: { show: false, formatter: l.label }, tooltip: { formatter: `${l.label}<br/>${l.kind} · ${Math.round(l.confidence * 100)}%` } })), force: { repulsion: 900, edgeLength: [90, 200], gravity: 0.08 }, emphasis: { focus: "adjacency", lineStyle: { width: 4 } }, edgeSymbol: ["none", "arrow"], edgeSymbolSize: 6 }],
    };
  }, [graph, filter, dark]);

  const decide = async (r: Rel, d: "approve" | "reject") => {
    await api(`/semantic/relationships/${r.id}/${d}`, { method: "POST" });
    load();
  };
  const discover = async () => {
    setBusy(true);
    try {
      const r = await api<{ suggested: number }>("/semantic/relationships/discover", { method: "POST" });
      alert(`${r.suggested} new relationship suggestion(s).`);
      await load();
    } finally {
      setBusy(false);
    }
  };
  const pending = (rels || []).filter((r) => r.status === "suggested");

  return (
    <div>
      <PageHeader title="Relationships" description="How tables connect within and across systems. The AI traverses this graph to decide which domains to investigate." actions={canEdit && <><Button variant="outline" icon={<Sparkles className="h-4 w-4" />} loading={busy} onClick={discover}>Discover relationships</Button><Button icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)}>Add manually</Button></>} />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "graph", label: "Knowledge graph" }, { id: "review", label: <span>Review suggestions {pending.length > 0 && <Badge tone="warning" className="ml-1">{pending.length}</Badge>}</span> }, { id: "all", label: `All (${rels?.length ?? 0})` }]} />
      {tab === "graph" && (
        <Card className="mt-4" padded={false} actions={<><Input className="w-52" placeholder="Highlight table / domain…" value={filter} onChange={(e) => setFilter(e.target.value)} /><Toggle checked={suggested} onChange={setSuggested} label="Include suggested" /></>} title="Interactive graph" subtitle="Drag to move, scroll to zoom. Dashed edges are entity-resolution links.">
          {!option ? <Spinner /> : <ReactECharts option={option} style={{ height: 620 }} notMerge />}
        </Card>
      )}
      {(tab === "review" || tab === "all") && (
        <Card className="mt-4" padded={false}>
          {!rels ? <Spinner /> : (
            <Table<Rel>
              rows={tab === "review" ? pending : rels}
              keyFn={(r) => r.id}
              columns={[
                { key: "from", label: "From", render: (r) => <div className="font-mono text-xs"><p>{r.from_table}.{r.from_column}</p><p className="font-sans text-[11px] text-muted">{r.from_source}</p></div> },
                { key: "to", label: "To", render: (r) => <div className="font-mono text-xs"><p>{r.to_table}.{r.to_column}</p><p className="font-sans text-[11px] text-muted">{r.to_source}</p></div> },
                { key: "type", label: "Type", render: (r) => <span className="text-xs">{r.type.replace(/_/g, "-")}{r.is_cross_source && <Badge tone="accent" className="ml-1">cross-source</Badge>}</span> },
                { key: "conf", label: "Confidence", render: (r) => <div className="flex items-center gap-2"><div className="h-1.5 w-16 overflow-hidden rounded-full bg-surface-2"><div className={`h-full ${r.confidence >= 0.8 ? "bg-success" : r.confidence >= 0.6 ? "bg-warning" : "bg-danger"}`} style={{ width: `${r.confidence * 100}%` }} /></div><span className="text-xs tabular-nums">{Math.round(r.confidence * 100)}%</span></div> },
                { key: "reason", label: "Reason", render: (r) => <span className="text-xs text-muted">{r.reason}</span> },
                { key: "status", label: "Status", render: (r) => <Badge tone={statusTone(r.status)}>{r.status}</Badge> },
                { key: "a", label: "", render: (r) => canEdit && r.status !== "approved" ? <div className="flex gap-1"><Button size="sm" variant="outline" icon={<Check className="h-3.5 w-3.5" />} onClick={() => decide(r, "approve")}>Approve</Button>{r.status !== "rejected" && <Button size="sm" variant="ghost" icon={<X className="h-3.5 w-3.5" />} onClick={() => decide(r, "reject")}>Reject</Button>}</div> : canEdit && r.status === "approved" ? <Button size="sm" variant="ghost" onClick={() => decide(r, "reject")}>Revoke</Button> : null },
              ]}
              empty={tab === "review" ? "No suggestions awaiting review. Run discovery after connecting sources." : "No relationships yet."}
            />
          )}
        </Card>
      )}
      <Modal open={creating} onClose={() => setCreating(false)} title="Create relationship"><ManualRel onDone={() => { setCreating(false); load(); }} /></Modal>
    </div>
  );
}

function ManualRel({ onDone }: { onDone: () => void }) {
  const [tables, setTables] = useState<{ id: string; table_name: string; source_name: string }[]>([]);
  const [a, setA] = useState({ t: "", c: "" });
  const [b, setB] = useState({ t: "", c: "" });
  const [ca, setCa] = useState<{ id: string; column_name: string }[]>([]);
  const [cb, setCb] = useState<{ id: string; column_name: string }[]>([]);
  const [type, setType] = useState("many_to_one");
  const [reason, setReason] = useState("");
  useEffect(() => {
    api<typeof tables>("/catalog/tables").then(setTables);
  }, []);
  useEffect(() => {
    if (a.t) api<{ columns: typeof ca }>(`/catalog/tables/${a.t}`).then((d) => setCa(d.columns));
  }, [a.t]);
  useEffect(() => {
    if (b.t) api<{ columns: typeof cb }>(`/catalog/tables/${b.t}`).then((d) => setCb(d.columns));
  }, [b.t]);
  return (
    <div className="space-y-3">
      {[{ l: "From", s: a, set: setA, cols: ca }, { l: "To", s: b, set: setB, cols: cb }].map(({ l, s, set, cols }) => (
        <div key={l} className="grid grid-cols-2 gap-3">
          <Field label={`${l} table`}><Select value={s.t} onChange={(e) => set({ t: e.target.value, c: "" })}><option value="">Select…</option>{tables.map((t) => <option key={t.id} value={t.id}>{t.source_name} · {t.table_name}</option>)}</Select></Field>
          <Field label={`${l} column`}><Select value={s.c} onChange={(e) => set({ ...s, c: e.target.value })}><option value="">Select…</option>{cols.map((c) => <option key={c.id} value={c.id}>{c.column_name}</option>)}</Select></Field>
        </div>
      ))}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Type"><Select value={type} onChange={(e) => setType(e.target.value)}>{["one_to_one", "one_to_many", "many_to_one", "many_to_many"].map((t) => <option key={t} value={t}>{t.replace(/_/g, "-")}</option>)}</Select></Field>
        <Field label="Reason"><Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Business key documented by Finance" /></Field>
      </div>
      <div className="flex justify-end"><Button disabled={!a.c || !b.c} onClick={() => api("/semantic/relationships", { method: "POST", json: { from_column_id: a.c, to_column_id: b.c, type, reason } }).then(onDone)}>Create (approved)</Button></div>
    </div>
  );
}
