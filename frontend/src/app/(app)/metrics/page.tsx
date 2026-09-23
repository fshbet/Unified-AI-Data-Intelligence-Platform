"use client";

import { api, fmtNumber } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Card, Code, Empty, Field, Input, Modal, PageHeader, Select, Spinner, Table, Textarea } from "@/components/ui";
import { Activity, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

type Metric = { id: string; name: string; display_name?: string | null; description?: string | null; table_id?: string | null; expression: string; filters?: string | null; date_column?: string | null; unit?: string | null; format: string; domain?: string | null; owner?: string | null; direction: string; related_metrics: string[]; dimensions: string[]; version: number; table_name?: string | null; source_name?: string | null; native_expression?: string | null; native_language?: string | null; is_computable?: boolean; source_system?: string | null };
type TableRow = { id: string; table_name: string; source_name: string };

export default function MetricsPage() {
  const { user } = useAuth();
  const [metrics, setMetrics] = useState<Metric[] | null>(null);
  const [edit, setEdit] = useState<Metric | null | "new">(null);
  const [preview, setPreview] = useState<{ m: Metric; result: Record<string, unknown> } | null>(null);
  const load = useCallback(() => api<Metric[]>("/semantic/metrics").then(setMetrics), []);
  useEffect(() => {
    load();
  }, [load]);
  const canEdit = user?.role !== "viewer";
  const run = async (m: Metric) => setPreview({ m, result: await api(`/semantic/metrics/${m.id}/preview`, { method: "POST" }) });
  return (
    <div>
      <PageHeader title="Metrics" description="Centralised metric catalog. Governed formulas the AI must use instead of inventing its own calculations. Every change is versioned." actions={canEdit && <Button icon={<Plus className="h-4 w-4" />} onClick={() => setEdit("new")}>Define metric</Button>} />
      {!metrics ? <Spinner /> : metrics.length === 0 ? <Empty icon={<Activity className="h-6 w-6" />} title="No metrics defined" description="Define Revenue, Orders, Attrition Rate… with formulas, filters, units and owners." /> : (
        <Card padded={false}>
          <Table<Metric>
            rows={metrics}
            keyFn={(m) => m.id}
            columns={[
              { key: "name", label: "Metric", render: (m) => (
                <div>
                  <p className="font-medium">{m.display_name || m.name}
                    {m.is_computable === false && <Badge tone="warning" className="ml-1.5">definition only</Badge>}
                  </p>
                  <p className="text-[11px] text-muted">{m.description?.slice(0, 110)}</p>
                  {m.source_system && <p className="mt-0.5 text-[10px] text-accent">imported from {m.source_system}</p>}
                </div>
              ) },
              { key: "formula", label: "Formula", render: (m) => (
                <div className="font-mono text-[11px]">
                  {m.is_computable === false ? (
                    <p className="text-muted">{m.native_expression || m.expression}</p>
                  ) : (
                    <>
                      <p>{m.expression}</p>
                      {m.filters && <p className="text-muted">WHERE {m.filters}</p>}
                    </>
                  )}
                  {m.native_expression && m.is_computable !== false && <p className="mt-0.5 text-muted opacity-80">{m.native_language?.toUpperCase()}: {m.native_expression}</p>}
                </div>
              ) },
              { key: "source", label: "Source", render: (m) => <span className="text-xs">{m.source_name} · <span className="font-mono">{m.table_name}</span></span> },
              { key: "domain", label: "Domain", render: (m) => m.domain ? <Badge tone="accent">{m.domain}</Badge> : "—" },
              { key: "dims", label: "Dimensions", render: (m) => <span className="text-[11px] text-muted">{m.dimensions.join(", ") || "auto"}</span> },
              { key: "owner", label: "Owner", render: (m) => <span className="text-xs">{m.owner || "—"}</span> },
              { key: "v", label: "Ver", render: (m) => <span className="text-xs text-muted">v{m.version}</span> },
              { key: "a", label: "", render: (m) => <div className="flex gap-1"><button title={m.is_computable === false ? "Definition only — cannot be computed" : "Preview"} disabled={m.is_computable === false} className="rounded p-1 text-muted hover:text-primary disabled:opacity-30" onClick={() => run(m)}><Play className="h-3.5 w-3.5" /></button>{canEdit && <button className="rounded p-1 text-muted hover:text-primary" onClick={() => setEdit(m)}><Pencil className="h-3.5 w-3.5" /></button>}{user?.role === "admin" && <button className="rounded p-1 text-muted hover:text-danger" onClick={() => confirm(`Delete metric ${m.name}?`) && api(`/semantic/metrics/${m.id}`, { method: "DELETE" }).then(load)}><Trash2 className="h-3.5 w-3.5" /></button>}</div> },
            ]}
          />
        </Card>
      )}
      <Modal open={!!edit} onClose={() => setEdit(null)} title={edit === "new" ? "Define metric" : `Edit ${(edit as Metric)?.name}`} width="max-w-2xl">
        {edit && <MetricForm initial={edit === "new" ? null : edit} onDone={() => { setEdit(null); load(); }} />}
      </Modal>
      <Modal open={!!preview} onClose={() => setPreview(null)} title={`Preview: ${preview?.m.display_name || preview?.m.name}`}>
        {preview && (
          <div className="space-y-3 text-sm">
            {preview.m.is_computable === false ? (
              <p className="rounded-lg bg-warning/10 px-3 py-2 text-xs text-warning">This metric was imported from {preview.m.source_system} as a definition only — its {preview.m.native_language?.toUpperCase()} formula has no exact SQL equivalent here, so no value can be computed.</p>
            ) : (
              <p className="text-2xl font-semibold">{fmtNumber(preview.result.value as number, { format: preview.m.format, unit: preview.m.unit })}<span className="ml-2 text-sm font-normal text-muted">{String(preview.result.period)}</span></p>
            )}
            {preview.result.error ? <p className="text-danger">{String(preview.result.error)}</p> : null}
            <p className="text-xs text-muted">Query {String(preview.result.query_ref)} · {String(preview.result.source)} · {String(preview.result.table)}</p>
          </div>
        )}
      </Modal>
    </div>
  );
}

function MetricForm({ initial, onDone }: { initial: Metric | null; onDone: () => void }) {
  const [tables, setTables] = useState<TableRow[]>([]);
  const [cols, setCols] = useState<{ column_name: string; logical_type: string; semantic_type?: string | null }[]>([]);
  const [f, setF] = useState({ name: initial?.name || "", display_name: initial?.display_name || "", description: initial?.description || "", table_id: initial?.table_id || "", expression: initial?.expression || "", filters: initial?.filters || "", date_column: initial?.date_column || "", unit: initial?.unit || "", format: initial?.format || "number", domain: initial?.domain || "", owner: initial?.owner || "", direction: initial?.direction || "up", related_metrics: (initial?.related_metrics || []).join(", "), dimensions: (initial?.dimensions || []).join(", ") });
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api<TableRow[]>("/catalog/tables").then(setTables);
  }, []);
  useEffect(() => {
    if (f.table_id) api<{ columns: typeof cols }>(`/catalog/tables/${f.table_id}`).then((d) => setCols(d.columns));
  }, [f.table_id]);
  const save = async () => {
    const body = { ...f, table_id: f.table_id || null, filters: f.filters || null, date_column: f.date_column || null, unit: f.unit || null, domain: f.domain || null, owner: f.owner || null, related_metrics: f.related_metrics.split(",").map((s) => s.trim()).filter(Boolean), dimensions: f.dimensions.split(",").map((s) => s.trim()).filter(Boolean) };
    try {
      if (initial) {
        const { name: _n, ...rest } = body; // eslint-disable-line @typescript-eslint/no-unused-vars
        await api(`/semantic/metrics/${initial.id}`, { method: "PATCH", json: rest });
      } else await api("/semantic/metrics", { method: "POST", json: body });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name (identifier)"><Input value={f.name} disabled={!!initial} onChange={(e) => setF({ ...f, name: e.target.value.toLowerCase().replace(/\s+/g, "_") })} placeholder="net_revenue" /></Field>
        <Field label="Display name"><Input value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} placeholder="Net Revenue" /></Field>
      </div>
      <Field label="Definition"><Textarea value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} placeholder="Recognised invoice revenue net of cancellations…" /></Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Table"><Select value={f.table_id} onChange={(e) => setF({ ...f, table_id: e.target.value })}><option value="">Select…</option>{tables.map((t) => <option key={t.id} value={t.id}>{t.source_name} · {t.table_name}</option>)}</Select></Field>
        <Field label="Date column" hint="Leave blank to use the table's time axis"><Select value={f.date_column} onChange={(e) => setF({ ...f, date_column: e.target.value })}><option value="">(table default)</option>{cols.filter((c) => ["date", "datetime"].includes(c.logical_type)).map((c) => <option key={c.column_name}>{c.column_name}</option>)}</Select></Field>
      </div>
      <Field label="Aggregate expression (SQL)" hint="e.g. SUM(invoice_amount), COUNT(*), 100.0 * AVG(delayed)"><Input className="font-mono" value={f.expression} onChange={(e) => setF({ ...f, expression: e.target.value })} /></Field>
      <Field label="Filters (SQL predicate)" hint="e.g. invoice_status = 'POSTED'. Use {period_start}/{period_end} for point-in-time metrics like headcount."><Input className="font-mono" value={f.filters} onChange={(e) => setF({ ...f, filters: e.target.value })} /></Field>
      <div className="grid grid-cols-4 gap-3">
        <Field label="Unit"><Input value={f.unit} onChange={(e) => setF({ ...f, unit: e.target.value })} placeholder="INR" /></Field>
        <Field label="Format"><Select value={f.format} onChange={(e) => setF({ ...f, format: e.target.value })}><option>number</option><option>currency</option><option>percent</option></Select></Field>
        <Field label="Good direction"><Select value={f.direction} onChange={(e) => setF({ ...f, direction: e.target.value })}><option value="up">higher is better</option><option value="down">lower is better</option></Select></Field>
        <Field label="Domain"><Input value={f.domain} onChange={(e) => setF({ ...f, domain: e.target.value })} /></Field>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <Field label="Owner"><Input value={f.owner} onChange={(e) => setF({ ...f, owner: e.target.value })} /></Field>
        <Field label="Dimensions (comma separated)" hint="Columns used for decomposition"><Input value={f.dimensions} onChange={(e) => setF({ ...f, dimensions: e.target.value })} placeholder="region, product_id" /></Field>
        <Field label="Related metrics"><Input value={f.related_metrics} onChange={(e) => setF({ ...f, related_metrics: e.target.value })} /></Field>
      </div>
      {f.expression && <Code>{`SELECT ${f.expression} AS value FROM ${tables.find((t) => t.id === f.table_id)?.table_name || "<table>"}${f.filters ? ` WHERE (${f.filters})` : ""}`}</Code>}
      {err && <p className="text-xs text-danger">{err}</p>}
      <div className="flex justify-end"><Button disabled={!f.name || !f.expression || !f.table_id} onClick={save}>{initial ? "Save (new version)" : "Create metric"}</Button></div>
    </div>
  );
}
