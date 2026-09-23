"use client";

import { api, parseDate, timeAgo } from "@/lib/api";
import { Badge, Button, Card, Field, Input, Modal, PageHeader, Select, Spinner, Stat, Table, Tabs, Textarea, statusTone } from "@/components/ui";
import { Chart } from "@/components/chart";
import { useAuth } from "@/lib/auth";
import { Check, Pencil, Sparkles } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

type Col = { id: string; column_name: string; data_type: string; logical_type: string; business_name?: string | null; description?: string | null; business_definition?: string | null; semantic_type?: string | null; unit?: string | null; is_primary_key: boolean; is_foreign_key: boolean; is_nullable: boolean; sensitivity: string; pii_type?: string | null; sample_values: unknown[]; stats: Record<string, unknown>; ai_suggestion?: Record<string, unknown> | null };
type Detail = {
  table: { id: string; table_name: string; schema_name?: string | null; business_name?: string | null; description?: string | null; business_domain?: string | null; row_count?: number | null; column_count?: number | null; last_profiled_at?: string | null; date_column?: string | null; source_name: string; source_id: string; ai_suggestion?: Record<string, unknown> | null; tags: string[] };
  columns: Col[];
  relationships: { id: string; from: string; to: string; type: string; confidence: number; status: string; reason?: string }[];
  issues: { id: string; rule: string; severity: string; message: string; status: string; detected_at: string }[];
  metrics: { id: string; name: string; display_name?: string; expression: string }[];
  profiles: { row_count: number; profiled_at: string; max_date?: string | null }[];
};

const SEMANTIC = ["identifier", "measure", "dimension", "date", "text", "flag"];
const SENS = ["public", "sensitive", "pii", "restricted"];

export default function TablePage() {
  const { id } = useParams<{ id: string }>();
  const { user } = useAuth();
  const [d, setD] = useState<Detail | null>(null);
  const [tab, setTab] = useState("columns");
  const [sample, setSample] = useState<{ columns: string[]; rows: unknown[][] } | null>(null);
  const [editCol, setEditCol] = useState<Col | null>(null);
  const [editTable, setEditTable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<Col | null>(null);
  const canEdit = user?.role !== "viewer";

  const load = useCallback(() => api<Detail>(`/catalog/tables/${id}`).then(setD), [id]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (tab === "sample" && !sample) api<{ columns: string[]; rows: unknown[][] }>(`/catalog/tables/${id}/sample?n=50`).then(setSample);
  }, [tab, sample, id]);

  if (!d) return <Spinner />;
  const t = d.table;
  const suggest = async () => {
    setBusy(true);
    try {
      const { job_id } = await api<{ job_id: string }>(`/catalog/tables/${id}/suggest`, { method: "POST" });
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const j = await api<{ status: string; message?: string }>(`/admin/jobs/${job_id}`);
        if (j.status === "done") break;
        if (j.status === "error") throw new Error(j.message || "AI suggestion failed");
      }
      await load();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageHeader
        title={t.business_name || t.table_name}
        description={t.description || "No description yet — add one or ask the AI to suggest."}
        actions={
          canEdit && (
            <>
              <Button variant="outline" icon={<Sparkles className="h-4 w-4" />} loading={busy} onClick={suggest}>AI suggest descriptions</Button>
              {(t.ai_suggestion || d.columns.some((c) => c.ai_suggestion)) && <Button variant="outline" icon={<Check className="h-4 w-4" />} onClick={() => api(`/catalog/tables/${id}/accept-suggestion`, { method: "POST" }).then(load)}>Accept all suggestions</Button>}
              <Button variant="outline" icon={<Pencil className="h-4 w-4" />} onClick={() => setEditTable(true)}>Edit</Button>
            </>
          )
        }
      />
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted">
        <span className="font-mono">{t.schema_name ? `${t.schema_name}.` : ""}{t.table_name}</span>·<Link href={`/sources/${t.source_id}`} className="text-primary hover:underline">{t.source_name}</Link>·{t.business_domain && <Badge tone="accent">{t.business_domain}</Badge>}
        {t.ai_suggestion && <Badge tone="info"><Sparkles className="h-3 w-3" /> AI suggestion: {String(t.ai_suggestion.description || "").slice(0, 120)}</Badge>}
      </div>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="Rows" value={t.row_count?.toLocaleString() ?? "—"} />
        <Stat label="Columns" value={t.column_count ?? d.columns.length} />
        <Stat label="Time axis" value={<span className="font-mono text-base">{t.date_column || "—"}</span>} />
        <Stat label="Last profiled" value={t.last_profiled_at ? timeAgo(t.last_profiled_at) : "never"} />
        <Stat label="Quality" value={d.issues.filter((i) => i.status === "open").length ? <span className="text-warning">{d.issues.filter((i) => i.status === "open").length} open</span> : <span className="text-success">Healthy</span>} sub={`${d.relationships.filter((r) => r.status === "approved").length} relationships · ${d.metrics.length} metrics`} />
      </div>
      <div className="mt-6">
        <Tabs value={tab} onChange={setTab} tabs={[{ id: "columns", label: `Columns (${d.columns.length})` }, { id: "sample", label: "Sample data" }, { id: "relationships", label: `Relationships (${d.relationships.length})` }, { id: "quality", label: `Data quality (${d.issues.length})` }, { id: "stats", label: "Statistics" }]} />
      </div>
      {tab === "columns" && (
        <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_360px]">
          <Card padded={false}>
            <Table<Col>
              rows={d.columns}
              keyFn={(c) => c.id}
              onRowClick={setSelected}
              dense
              columns={[
                { key: "name", label: "Column", render: (c) => <div><p className="font-mono text-xs font-medium">{c.column_name}{c.is_primary_key && <span className="ml-1 text-[9px] text-primary">PK</span>}{c.is_foreign_key && <span className="ml-1 text-[9px] text-accent">FK</span>}</p><p className="text-[11px] text-muted">{c.business_name}</p></div> },
                { key: "type", label: "Type", render: (c) => <span className="font-mono text-[11px] text-muted">{c.data_type}</span> },
                { key: "sem", label: "Semantic", render: (c) => <Badge tone={c.semantic_type === "measure" ? "info" : c.semantic_type === "dimension" ? "accent" : c.semantic_type === "date" ? "success" : "neutral"}>{c.semantic_type || "—"}</Badge> },
                { key: "desc", label: "Description", render: (c) => <div className="text-xs"><span className="text-text/80">{c.business_definition || c.description || <span className="text-muted">—</span>}</span>{c.ai_suggestion && !c.business_definition && <p className="mt-0.5 text-[11px] text-primary"><Sparkles className="mr-1 inline h-3 w-3" />{String(c.ai_suggestion.description)}</p>}</div> },
                { key: "sens", label: "Sensitivity", render: (c) => <Badge tone={statusTone(c.sensitivity)}>{c.pii_type || c.sensitivity}</Badge> },
                { key: "null", label: "Null %", align: "right", render: (c) => `${Number(c.stats.null_percentage ?? 0).toFixed(1)}%` },
                { key: "dist", label: "Distinct", align: "right", render: (c) => Number(c.stats.distinct_count ?? 0).toLocaleString() },
                { key: "edit", label: "", render: (c) => canEdit && <button className="text-muted hover:text-primary" onClick={(e) => { e.stopPropagation(); setEditCol(c); }}><Pencil className="h-3.5 w-3.5" /></button> },
              ]}
            />
          </Card>
          <div>{selected ? <ColumnProfile c={selected} /> : <Card><p className="text-sm text-muted">Select a column to see its full profile.</p></Card>}</div>
        </div>
      )}
      {tab === "sample" && (
        <Card className="mt-4" padded={false}>
          {!sample ? <Spinner /> : <div className="overflow-x-auto"><table className="w-full text-xs"><thead><tr className="border-b bg-surface-2/50">{sample.columns.map((c) => <th key={c} className="px-2 py-1.5 text-left font-mono font-medium">{c}</th>)}</tr></thead><tbody>{sample.rows.map((r, i) => <tr key={i} className="border-b last:border-b-0">{r.map((v, j) => <td key={j} className="max-w-[240px] truncate px-2 py-1 font-mono">{v === null ? <span className="text-muted">null</span> : String(v)}</td>)}</tr>)}</tbody></table></div>}
        </Card>
      )}
      {tab === "relationships" && (
        <Card className="mt-4" padded={false}>
          <Table rows={d.relationships} keyFn={(r) => r.id} columns={[{ key: "from", label: "From", render: (r) => <span className="font-mono text-xs">{r.from}</span> }, { key: "to", label: "To", render: (r) => <span className="font-mono text-xs">{r.to}</span> }, { key: "type", label: "Type" }, { key: "conf", label: "Confidence", render: (r) => `${Math.round(r.confidence * 100)}%` }, { key: "status", label: "Status", render: (r) => <Badge tone={statusTone(r.status)}>{r.status}</Badge> }, { key: "reason", label: "Reason", render: (r) => <span className="text-xs text-muted">{r.reason}</span> }]} empty={<span>No relationships. Discover them from the <Link className="text-primary underline" href="/relationships">Relationships</Link> page.</span>} />
        </Card>
      )}
      {tab === "quality" && (
        <Card className="mt-4" padded={false}>
          <Table rows={d.issues} keyFn={(i) => i.id} columns={[{ key: "sev", label: "Severity", render: (i) => <Badge tone={statusTone(i.severity)}>{i.severity}</Badge> }, { key: "rule", label: "Rule", render: (i) => i.rule.replace(/_/g, " ") }, { key: "message", label: "Message" }, { key: "status", label: "Status", render: (i) => <Badge tone={statusTone(i.status)}>{i.status}</Badge> }, { key: "when", label: "Detected", render: (i) => timeAgo(i.detected_at) }]} empty="No data-quality issues detected." />
        </Card>
      )}
      {tab === "stats" && (
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <Card title="Row count history"><Chart spec={{ type: "line", x: d.profiles.map((p) => parseDate(p.profiled_at).toLocaleString()), series: [{ name: "rows", data: d.profiles.map((p) => p.row_count) }] }} height={220} /></Card>
          <Card title="Governed metrics on this table" padded={false}>
            <Table rows={d.metrics} keyFn={(m) => m.id} dense columns={[{ key: "name", label: "Metric", render: (m) => <Link href="/metrics" className="text-primary hover:underline">{m.display_name || m.name}</Link> }, { key: "expression", label: "Formula", render: (m) => <span className="font-mono text-xs">{m.expression}</span> }]} empty="No metrics defined on this table." />
          </Card>
        </div>
      )}

      <Modal open={!!editCol} onClose={() => setEditCol(null)} title={`Edit column ${editCol?.column_name}`}>
        {editCol && <ColumnEditor c={editCol} onSaved={() => { setEditCol(null); load(); }} onClose={() => setEditCol(null)} />}
      </Modal>
      <Modal open={editTable} onClose={() => setEditTable(false)} title="Edit table metadata">
        <TableEditor t={t} columns={d.columns} onSaved={() => { setEditTable(false); load(); }} onClose={() => setEditTable(false)} />
      </Modal>
    </div>
  );
}

function ColumnProfile({ c }: { c: Col }) {
  const s = c.stats;
  const freq = (s.frequency_distribution as { value: string; count: number }[] | undefined) || [];
  const num = (k: string) => (s[k] !== undefined && s[k] !== null ? Number(s[k]).toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—");
  return (
    <Card title={<span className="font-mono">{c.column_name}</span>} subtitle={c.business_name || undefined}>
      <p className="text-xs text-text/80">{c.business_definition || c.description || "No description."}</p>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
        {[["Type", c.data_type], ["Logical", c.logical_type], ["Semantic", c.semantic_type], ["Unit", c.unit], ["Sensitivity", c.pii_type ? `${c.sensitivity} (${c.pii_type})` : c.sensitivity], ["Nullable", c.is_nullable ? "yes" : "no"], ["Rows", num("count")], ["Nulls", `${num("null_count")} (${num("null_percentage")}%)`], ["Distinct", num("distinct_count")], ["Duplicates", num("duplicate_count")], ["Min", num("min")], ["Max", num("max")], ["Mean", num("mean")], ["Median", num("median")], ["Std dev", num("std")], ["Outliers", num("outlier_count")]].map(([k, v]) => (
          <div key={k as string} className="flex justify-between border-b border-dashed py-1"><dt className="text-muted">{k}</dt><dd className="font-mono">{(v as string) || "—"}</dd></div>
        ))}
      </dl>
      {s.percentiles ? <p className="mt-2 text-[11px] text-muted">Percentiles: {Object.entries(s.percentiles as Record<string, number>).map(([k, v]) => `${k}=${Number(v).toLocaleString()}`).join(" · ")}</p> : null}
      {c.sample_values.length > 0 && <div className="mt-3"><p className="mb-1 text-[11px] font-medium text-muted">Sample values</p><div className="flex flex-wrap gap-1">{c.sample_values.map((v, i) => <span key={i} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11px]">{String(v)}</span>)}</div></div>}
      {freq.length > 0 && <div className="mt-3"><Chart spec={{ type: "bar", title: "Frequency distribution", x: freq.slice(0, 12).map((f) => f.value), series: [{ name: "count", data: freq.slice(0, 12).map((f) => f.count) }] }} height={180} /></div>}
      {c.ai_suggestion && <div className="mt-3 rounded-lg border border-primary/30 bg-primary/5 p-2 text-xs"><p className="mb-1 flex items-center gap-1 font-medium text-primary"><Sparkles className="h-3 w-3" /> AI suggestion</p><p>{String(c.ai_suggestion.description)}</p><p className="mt-1 text-muted">{String(c.ai_suggestion.semantic_type || "")} {c.ai_suggestion.unit ? `· ${c.ai_suggestion.unit}` : ""}</p><Button size="sm" className="mt-2" onClick={() => api(`/catalog/columns/${c.id}/accept-suggestion`, { method: "POST" }).then(() => location.reload())}>Accept</Button></div>}
    </Card>
  );
}

function ColumnEditor({ c, onSaved, onClose }: { c: Col; onSaved: () => void; onClose: () => void }) {
  const [f, setF] = useState({ business_name: c.business_name || "", description: c.description || "", business_definition: c.business_definition || "", semantic_type: c.semantic_type || "", unit: c.unit || "", sensitivity: c.sensitivity, pii_type: c.pii_type || "" });
  const [busy, setBusy] = useState(false);
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Business name"><Input value={f.business_name} onChange={(e) => setF({ ...f, business_name: e.target.value })} /></Field>
        <Field label="Unit"><Input value={f.unit} onChange={(e) => setF({ ...f, unit: e.target.value })} placeholder="INR, %, days…" /></Field>
        <Field label="Semantic type"><Select value={f.semantic_type} onChange={(e) => setF({ ...f, semantic_type: e.target.value })}><option value="">—</option>{SEMANTIC.map((s) => <option key={s}>{s}</option>)}</Select></Field>
        <Field label="Sensitivity"><Select value={f.sensitivity} onChange={(e) => setF({ ...f, sensitivity: e.target.value })}>{SENS.map((s) => <option key={s}>{s}</option>)}</Select></Field>
      </div>
      <Field label="Description"><Textarea value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <Field label="Business definition" hint="Authoritative meaning used by the AI, e.g. 'Total recognised revenue from customer transactions'."><Textarea value={f.business_definition} onChange={(e) => setF({ ...f, business_definition: e.target.value })} /></Field>
      {c.ai_suggestion && <button className="text-xs text-primary hover:underline" onClick={() => setF({ ...f, business_name: String(c.ai_suggestion!.business_name || f.business_name), description: String(c.ai_suggestion!.description || f.description), semantic_type: String(c.ai_suggestion!.semantic_type || f.semantic_type), unit: String(c.ai_suggestion!.unit || f.unit) })}><Sparkles className="mr-1 inline h-3 w-3" />Use AI suggestion</button>}
      <div className="flex justify-end gap-2"><Button variant="ghost" onClick={onClose}>Cancel</Button><Button loading={busy} onClick={async () => { setBusy(true); await api(`/catalog/columns/${c.id}`, { method: "PATCH", json: { ...f, pii_type: f.pii_type || null, semantic_type: f.semantic_type || null } }); onSaved(); }}>Save (creates version)</Button></div>
    </div>
  );
}

function TableEditor({ t, columns, onSaved, onClose }: { t: Detail["table"]; columns: Col[]; onSaved: () => void; onClose: () => void }) {
  const [f, setF] = useState({ business_name: t.business_name || "", description: t.description || "", business_domain: t.business_domain || "", date_column: t.date_column || "" });
  return (
    <div className="space-y-3">
      <Field label="Business name"><Input value={f.business_name} onChange={(e) => setF({ ...f, business_name: e.target.value })} /></Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Business domain"><Input value={f.business_domain} onChange={(e) => setF({ ...f, business_domain: e.target.value })} placeholder="finance, sales, hr…" /></Field>
        <Field label="Primary time axis" hint="Date column used for period analysis"><Select value={f.date_column} onChange={(e) => setF({ ...f, date_column: e.target.value })}><option value="">—</option>{columns.filter((c) => ["date", "datetime"].includes(c.logical_type)).map((c) => <option key={c.id}>{c.column_name}</option>)}</Select></Field>
      </div>
      <Field label="Description"><Textarea value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <div className="flex justify-end gap-2"><Button variant="ghost" onClick={onClose}>Cancel</Button><Button onClick={async () => { await api(`/catalog/tables/${t.id}`, { method: "PATCH", json: { ...f, date_column: f.date_column || null } }); onSaved(); }}>Save</Button></div>
    </div>
  );
}
