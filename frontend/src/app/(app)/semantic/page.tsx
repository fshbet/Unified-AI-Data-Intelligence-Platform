"use client";

import { api, parseDate } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Card, Empty, Field, Input, Modal, PageHeader, Select, Spinner, Tabs, Textarea } from "@/components/ui";
import { Layers, Plus, Search, Sparkles, Trash2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

type Entity = { id: string; name: string; description?: string | null; domain?: string | null; mappings: { id: string; column_id: string; table: string; column: string; source: string; role: string; confidence: number }[] };
type Hit = { type: string; table?: string; column?: string; name?: string; term?: string; description?: string; definition?: string; semantic_type?: string; expression?: string; score: number; source?: string; domain?: string };
type Version = { id: string; object_type: string; object_name: string; version: number; changed_by: string; change_summary: string; previous: Record<string, unknown> | null; current: Record<string, unknown>; created_at: string };
type Col = { id: string; column_name: string; table_id: string };
type TableRow = { id: string; table_name: string; source_name: string };

export default function SemanticPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState("entities");
  const [entities, setEntities] = useState<Entity[] | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [versions, setVersions] = useState<Version[]>([]);
  const [newEnt, setNewEnt] = useState(false);
  const [mapEnt, setMapEnt] = useState<Entity | null>(null);
  const canEdit = user?.role !== "viewer";
  const load = useCallback(() => api<Entity[]>("/semantic/entities").then(setEntities), []);
  useEffect(() => {
    load();
    api<Version[]>("/semantic/versions?limit=100").then(setVersions);
  }, [load]);
  useEffect(() => {
    if (!q.trim()) return setHits([]);
    const t = setTimeout(() => api<{ results: Hit[] }>(`/semantic/search?q=${encodeURIComponent(q)}&limit=20`).then((r) => setHits(r.results)), 250);
    return () => clearTimeout(t);
  }, [q]);

  return (
    <div>
      <PageHeader title="Semantic Layer" description="The unified business model: canonical entities resolved across systems, semantic search over every definition, and full change history." actions={canEdit && <><Button variant="outline" icon={<Sparkles className="h-4 w-4" />} onClick={() => api("/semantic/entities/suggest", { method: "POST" }).then(load)}>Auto-suggest entities</Button><Button icon={<Plus className="h-4 w-4" />} onClick={() => setNewEnt(true)}>New entity</Button></>} />
      <Card className="mb-6">
        <div className="relative"><Search className="absolute left-3 top-2.5 h-4 w-4 text-muted" /><Input className="h-10 pl-9 text-base" placeholder="Search the semantic catalog — e.g. 'employee attrition', 'revenue recognition', 'product availability'…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        {hits.length > 0 && (
          <ul className="mt-3 divide-y">
            {hits.map((h, i) => (
              <li key={i} className="flex items-start gap-3 py-2 text-sm">
                <Badge tone={h.type === "metric" ? "info" : h.type === "glossary" ? "accent" : h.type === "table" ? "success" : "neutral"}>{h.type}</Badge>
                <div className="min-w-0 flex-1">
                  <p className="font-mono text-xs font-medium">{h.type === "column" ? `${h.table}.${h.column}` : h.type === "table" ? h.table : h.type === "metric" ? h.name : h.term}{h.source && <span className="ml-2 font-sans text-muted">{h.source}</span>}</p>
                  <p className="text-xs text-muted">{h.description || h.definition || h.expression || "—"}</p>
                </div>
                <span className="text-[11px] text-muted">{(h.score * 100).toFixed(0)}%</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "entities", label: "Entities & entity resolution" }, { id: "versions", label: `Model versions (${versions.length})` }]} />
      {tab === "entities" && (
        <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {!entities ? <Spinner /> : entities.length === 0 ? <div className="md:col-span-3"><Empty icon={<Layers className="h-6 w-6" />} title="No canonical entities" description="Entities map identifiers like customer_id / customer_code / client_id to one canonical Customer. Auto-suggest them from the catalog or create manually." /></div> : entities.map((e) => (
            <Card key={e.id} title={e.name} subtitle={e.description || undefined} actions={canEdit && <><Button size="sm" variant="ghost" onClick={() => setMapEnt(e)}>+ mapping</Button>{user?.role === "admin" && <button className="text-muted hover:text-danger" onClick={() => confirm(`Delete entity ${e.name}?`) && api(`/semantic/entities/${e.id}`, { method: "DELETE" }).then(load)}><Trash2 className="h-3.5 w-3.5" /></button>}</>}>
              <ul className="space-y-1.5">
                {e.mappings.map((m) => (
                  <li key={m.id} className="flex items-center justify-between rounded-lg bg-surface-2/60 px-2.5 py-1.5 text-xs">
                    <div><span className="font-mono">{m.table}.{m.column}</span><span className="ml-2 text-muted">{m.source}</span></div>
                    <div className="flex items-center gap-2"><span className="text-muted">{Math.round(m.confidence * 100)}%</span>{canEdit && <button className="text-muted hover:text-danger" onClick={() => api(`/semantic/entities/${e.id}/mappings/${m.id}`, { method: "DELETE" }).then(load)}>×</button>}</div>
                  </li>
                ))}
                {e.mappings.length === 0 && <li className="text-xs text-muted">No mappings yet.</li>}
              </ul>
            </Card>
          ))}
        </div>
      )}
      {tab === "versions" && (
        <Card className="mt-4" padded={false}>
          <ul className="divide-y">
            {versions.map((v) => (
              <li key={v.id} className="px-5 py-3 text-sm">
                <div className="flex items-center gap-2"><Badge tone="info">{v.object_type}</Badge><span className="font-medium">{v.object_name}</span><span className="text-muted">v{v.version}</span><span className="ml-auto text-xs text-muted">{v.changed_by} · {parseDate(v.created_at).toLocaleString()}</span></div>
                <p className="mt-1 text-xs text-muted">{v.change_summary}</p>
                {v.previous && (
                  <div className="mt-2 grid gap-2 md:grid-cols-2">
                    {Object.keys(v.current).filter((k) => JSON.stringify(v.current[k]) !== JSON.stringify(v.previous?.[k])).map((k) => (
                      <div key={k} className="rounded-lg border bg-surface-2/40 p-2 text-xs"><p className="font-mono font-medium">{k}</p><p className="text-danger/80 line-through">{String(v.previous?.[k] ?? "∅")}</p><p className="text-success">{String(v.current[k] ?? "∅")}</p></div>
                    ))}
                  </div>
                )}
              </li>
            ))}
            {versions.length === 0 && <li className="px-5 py-8 text-center text-sm text-muted">No changes recorded yet. Edits to metrics, column definitions, relationships and glossary terms create versions.</li>}
          </ul>
        </Card>
      )}
      <Modal open={newEnt} onClose={() => setNewEnt(false)} title="New canonical entity"><EntityForm onDone={() => { setNewEnt(false); load(); }} /></Modal>
      <Modal open={!!mapEnt} onClose={() => setMapEnt(null)} title={`Map a column to ${mapEnt?.name}`}>{mapEnt && <MappingForm entity={mapEnt} onDone={() => { setMapEnt(null); load(); }} />}</Modal>
      <p className="mt-6 text-xs text-muted">Relationships between tables live on the <Link href="/relationships" className="text-primary hover:underline">Relationships</Link> page; governed measures on <Link href="/metrics" className="text-primary hover:underline">Metrics</Link>.</p>
    </div>
  );
}

function EntityForm({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState({ name: "", description: "", domain: "" });
  return (
    <div className="space-y-3">
      <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Customer" /></Field>
      <Field label="Domain"><Input value={f.domain} onChange={(e) => setF({ ...f, domain: e.target.value })} /></Field>
      <Field label="Description"><Textarea value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <div className="flex justify-end"><Button disabled={!f.name} onClick={() => api("/semantic/entities", { method: "POST", json: f }).then(onDone)}>Create</Button></div>
    </div>
  );
}

function MappingForm({ entity, onDone }: { entity: Entity; onDone: () => void }) {
  const [tables, setTables] = useState<TableRow[]>([]);
  const [cols, setCols] = useState<Col[]>([]);
  const [tableId, setTableId] = useState("");
  const [colId, setColId] = useState("");
  useEffect(() => {
    api<TableRow[]>("/catalog/tables").then(setTables);
  }, []);
  useEffect(() => {
    if (tableId) api<{ columns: Col[] }>(`/catalog/tables/${tableId}`).then((d) => setCols(d.columns));
  }, [tableId]);
  return (
    <div className="space-y-3">
      <Field label="Table"><Select value={tableId} onChange={(e) => setTableId(e.target.value)}><option value="">Select…</option>{tables.map((t) => <option key={t.id} value={t.id}>{t.source_name} · {t.table_name}</option>)}</Select></Field>
      <Field label="Column"><Select value={colId} onChange={(e) => setColId(e.target.value)}><option value="">Select…</option>{cols.map((c) => <option key={c.id} value={c.id}>{c.column_name}</option>)}</Select></Field>
      <div className="flex justify-end"><Button disabled={!colId} onClick={() => api(`/semantic/entities/${entity.id}/mappings`, { method: "POST", json: { column_id: colId } }).then(onDone)}>Add mapping</Button></div>
    </div>
  );
}
