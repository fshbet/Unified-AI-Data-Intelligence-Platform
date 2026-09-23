"use client";

import { api, timeAgo } from "@/lib/api";
import { Badge, Button, Card, Field, Input, Modal, PageHeader, Select, Spinner, Stat, Table, Tabs, Toggle, statusTone } from "@/components/ui";
import { Chart } from "@/components/chart";
import { CheckCircle2, Plus, Trash2, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

type Provider = { id: string; name: string; provider: string; model: string; api_key?: string | null; base_url?: string | null; temperature: number; max_tokens: number; embedding_model?: string | null; purposes: string[]; is_default: boolean; input_cost_per_1m: number; output_cost_per_1m: number; extra: Record<string, unknown> };
type PType = { provider: string; base_url: string; purposes: string[] };
type U = { id: string; email: string; name: string; role: string; department?: string | null; is_active: boolean };
type Perm = { id: string; role: string; resource_type: string; resource_id: string; resource_name?: string | null; access: string; row_filter?: string | null; mask_columns: boolean };
type Usage = { today: { requests: number; tokens: number; cost: number }; period: { requests: number; tokens: number; cost: number; avg_latency_ms: number }; by_model: { model: string; requests: number; input_tokens: number; output_tokens: number; cost: number }[]; by_day: { day: string; requests: number; tokens: number; cost: number }[]; most_expensive?: { question?: string | null; model: string; tokens: number; cost: number } | null; recent: { id: string; created_at: string; model: string; purpose: string; input_tokens: number; output_tokens: number; cost: number; duration_ms: number; question?: string | null }[] };
type Mon = { queries: { total: number; by_status: Record<string, number>; p50_ms: number; p95_ms: number; errors: { ref: string; error: string; created_at: string }[] }; ai: { requests: number; avg_latency_ms: number; tokens: number }; jobs: { total: number; failed: { id: string; kind: string; message: string; created_at: string }[]; running: number }; sources: { name: string; status: string; last_error?: string | null }[] };

export default function AdminPage() {
  const [tab, setTab] = useState("ai");
  return (
    <div>
      <PageHeader title="Administration" description="AI providers, users, access control, AI usage & cost, and platform monitoring." />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "ai", label: "AI Providers" }, { id: "usage", label: "AI Usage & Cost" }, { id: "users", label: "Users" }, { id: "perms", label: "Permissions" }, { id: "mon", label: "Monitoring" }]} />
      <div className="mt-4">{tab === "ai" && <Providers />}{tab === "usage" && <UsagePanel />}{tab === "users" && <Users />}{tab === "perms" && <Permissions />}{tab === "mon" && <Monitoring />}</div>
    </div>
  );
}

function Providers() {
  const [list, setList] = useState<Provider[] | null>(null);
  const [types, setTypes] = useState<PType[]>([]);
  const [edit, setEdit] = useState<Provider | "new" | null>(null);
  const [test, setTest] = useState<Record<string, { ok: boolean; message: string; latency_ms?: number; embedding_dims?: number; embedding_error?: string }>>({});
  const load = useCallback(() => api<Provider[]>("/admin/ai/providers").then(setList), []);
  useEffect(() => {
    load();
    api<PType[]>("/admin/ai/provider-types").then(setTypes);
  }, [load]);
  return (
    <div>
      <div className="mb-3 flex justify-end"><Button icon={<Plus className="h-4 w-4" />} onClick={() => setEdit("new")}>Add provider</Button></div>
      {!list ? <Spinner /> : (
        <div className="grid gap-4 md:grid-cols-2">
          {list.map((p) => (
            <Card key={p.id} title={<span>{p.name} {p.is_default && <Badge tone="info">default</Badge>}</span>} subtitle={`${p.provider} · ${p.model}${p.embedding_model ? ` · embeddings: ${p.embedding_model}` : ""}`} actions={<><Button size="sm" variant="outline" onClick={async () => setTest({ ...test, [p.id]: await api(`/admin/ai/providers/${p.id}/test`, { method: "POST" }) })}>Test</Button><Button size="sm" variant="ghost" onClick={() => setEdit(p)}>Edit</Button><button className="text-muted hover:text-danger" onClick={() => confirm("Delete provider?") && api(`/admin/ai/providers/${p.id}`, { method: "DELETE" }).then(load)}><Trash2 className="h-3.5 w-3.5" /></button></>}>
              <dl className="grid grid-cols-2 gap-1 text-xs"><dt className="text-muted">Base URL</dt><dd className="truncate font-mono">{p.base_url || "default"}</dd><dt className="text-muted">API key</dt><dd className="font-mono">{p.api_key || "—"}</dd><dt className="text-muted">Temperature / max tokens</dt><dd>{p.temperature} / {p.max_tokens}</dd><dt className="text-muted">Used for</dt><dd>{p.purposes.map((x) => <Badge key={x} className="mr-1">{x}</Badge>)}</dd><dt className="text-muted">Cost / 1M tokens</dt><dd>${p.input_cost_per_1m} in · ${p.output_cost_per_1m} out</dd></dl>
              {test[p.id] && <p className={`mt-3 flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs ${test[p.id].ok ? "bg-success/10 text-success" : "bg-danger/10 text-danger"}`}>{test[p.id].ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}{test[p.id].message} {test[p.id].latency_ms && `· ${test[p.id].latency_ms} ms`} {test[p.id].embedding_dims && `· embeddings ok (${test[p.id].embedding_dims} dims)`} {test[p.id].embedding_error && `· embeddings: ${test[p.id].embedding_error}`}</p>}
            </Card>
          ))}
          {list.length === 0 && <p className="text-sm text-muted">No AI provider configured. The assistant will fall back to deterministic analysis until one is added.</p>}
        </div>
      )}
      <Modal open={!!edit} onClose={() => setEdit(null)} title={edit === "new" ? "Add AI provider" : "Edit provider"} width="max-w-2xl">{edit && <ProviderForm types={types} initial={edit === "new" ? null : edit} onDone={() => { setEdit(null); load(); }} />}</Modal>
    </div>
  );
}

function ProviderForm({ types, initial, onDone }: { types: PType[]; initial: Provider | null; onDone: () => void }) {
  const [f, setF] = useState({ name: initial?.name || "", provider: initial?.provider || "openai", model: initial?.model || "", api_key: initial?.api_key || "", base_url: initial?.base_url || "", temperature: initial?.temperature ?? 0.1, max_tokens: initial?.max_tokens ?? 4096, embedding_model: initial?.embedding_model || "", purposes: initial?.purposes || ["reasoning", "planning", "metadata", "summarization"], is_default: initial?.is_default ?? true, input_cost_per_1m: initial?.input_cost_per_1m ?? 0, output_cost_per_1m: initial?.output_cost_per_1m ?? 0 });
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const pt = types.find((t) => t.provider === f.provider);
  const body = { ...f, api_key: f.api_key || null, base_url: f.base_url || pt?.base_url || null, embedding_model: f.embedding_model || null, extra: initial?.extra || {} };
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Production OpenAI" /></Field>
        <Field label="Provider"><Select value={f.provider} onChange={(e) => setF({ ...f, provider: e.target.value, base_url: "" })}>{types.map((t) => <option key={t.provider} value={t.provider}>{t.provider}</option>)}</Select></Field>
        <Field label="Model"><Input value={f.model} onChange={(e) => setF({ ...f, model: e.target.value })} placeholder={f.provider === "ollama" ? "qwen3:8b" : f.provider === "anthropic" ? "claude-sonnet-4-5" : "gpt-4o"} /></Field>
        <Field label="Embedding model (optional)"><Input value={f.embedding_model} onChange={(e) => setF({ ...f, embedding_model: e.target.value })} placeholder="text-embedding-3-small / nomic-embed-text" /></Field>
        <Field label="API key"><Input type="password" value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value })} placeholder={f.provider === "ollama" ? "not required" : "sk-…"} /></Field>
        <Field label="Base URL" hint={`Default: ${pt?.base_url || ""}`}><Input value={f.base_url} onChange={(e) => setF({ ...f, base_url: e.target.value })} placeholder={pt?.base_url} /></Field>
        <Field label="Temperature"><Input type="number" step="0.1" min={0} max={2} value={f.temperature} onChange={(e) => setF({ ...f, temperature: Number(e.target.value) })} /></Field>
        <Field label="Max tokens"><Input type="number" value={f.max_tokens} onChange={(e) => setF({ ...f, max_tokens: Number(e.target.value) })} /></Field>
        <Field label="Input cost / 1M tokens ($)"><Input type="number" step="0.01" value={f.input_cost_per_1m} onChange={(e) => setF({ ...f, input_cost_per_1m: Number(e.target.value) })} /></Field>
        <Field label="Output cost / 1M tokens ($)"><Input type="number" step="0.01" value={f.output_cost_per_1m} onChange={(e) => setF({ ...f, output_cost_per_1m: Number(e.target.value) })} /></Field>
      </div>
      <Field label="Use this provider for" hint="Different providers can serve different purposes (e.g. a cheap model for metadata, a strong one for reasoning).">
        <div className="flex flex-wrap gap-3">{(pt?.purposes || ["metadata", "embeddings", "planning", "reasoning", "summarization"]).map((p) => <Toggle key={p} checked={f.purposes.includes(p)} onChange={(v) => setF({ ...f, purposes: v ? [...f.purposes, p] : f.purposes.filter((x) => x !== p) })} label={p} />)}</div>
      </Field>
      <Toggle checked={f.is_default} onChange={(v) => setF({ ...f, is_default: v })} label="Default provider" />
      {test && <p className={`rounded-lg px-3 py-2 text-xs ${test.ok ? "bg-success/10 text-success" : "bg-danger/10 text-danger"}`}>{test.message}</p>}
      <div className="flex justify-end gap-2"><Button variant="outline" loading={busy === "test"} onClick={async () => { setBusy("test"); try { setTest(await api("/admin/ai/providers/test", { method: "POST", json: body })); } finally { setBusy(null); } }}>Test connection</Button><Button loading={busy === "save"} disabled={!f.name || !f.model} onClick={async () => { setBusy("save"); try { if (initial) await api(`/admin/ai/providers/${initial.id}`, { method: "PUT", json: body }); else await api("/admin/ai/providers", { method: "POST", json: body }); onDone(); } catch (e) { setTest({ ok: false, message: (e as Error).message }); } finally { setBusy(null); } }}>Save</Button></div>
    </div>
  );
}

function UsagePanel() {
  const [u, setU] = useState<Usage | null>(null);
  useEffect(() => {
    api<Usage>("/admin/ai/usage?days=30").then(setU);
  }, []);
  if (!u) return <Spinner />;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="Requests today" value={u.today.requests} sub={`${u.today.tokens.toLocaleString()} tokens`} />
        <Stat label="Cost today" value={`$${u.today.cost.toFixed(4)}`} />
        <Stat label="Requests (30d)" value={u.period.requests} sub={`${u.period.tokens.toLocaleString()} tokens`} />
        <Stat label="Cost (30d)" value={`$${u.period.cost.toFixed(4)}`} sub={`avg latency ${u.period.avg_latency_ms} ms`} />
        <Stat label="Most used model" value={<span className="text-base">{u.by_model[0]?.model || "—"}</span>} sub={u.most_expensive ? `Most expensive: ${u.most_expensive.tokens.toLocaleString()} tokens` : undefined} />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Daily tokens"><Chart spec={{ type: "bar", x: u.by_day.map((d) => d.day), series: [{ name: "tokens", data: u.by_day.map((d) => d.tokens) }] }} height={220} /></Card>
        <Card title="By model" padded={false}><Table rows={u.by_model} keyFn={(r) => r.model} dense columns={[{ key: "model", label: "Model" }, { key: "requests", label: "Requests", align: "right" }, { key: "input_tokens", label: "In", align: "right", render: (r) => r.input_tokens.toLocaleString() }, { key: "output_tokens", label: "Out", align: "right", render: (r) => r.output_tokens.toLocaleString() }, { key: "cost", label: "Cost", align: "right", render: (r) => `$${r.cost.toFixed(4)}` }]} /></Card>
      </div>
      <Card title="Recent requests" padded={false}><Table rows={u.recent} keyFn={(r) => r.id} dense columns={[{ key: "when", label: "When", render: (r) => timeAgo(r.created_at) }, { key: "model", label: "Model" }, { key: "purpose", label: "Purpose" }, { key: "q", label: "Question", render: (r) => <span className="line-clamp-1 max-w-md text-xs text-muted">{r.question}</span> }, { key: "tok", label: "Tokens", align: "right", render: (r) => `${r.input_tokens.toLocaleString()} / ${r.output_tokens.toLocaleString()}` }, { key: "ms", label: "ms", align: "right", render: (r) => r.duration_ms }, { key: "cost", label: "Cost", align: "right", render: (r) => `$${r.cost.toFixed(5)}` }]} /></Card>
    </div>
  );
}

function Users() {
  const [list, setList] = useState<U[] | null>(null);
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ email: "", name: "", password: "", role: "analyst", department: "" });
  const load = useCallback(() => api<U[]>("/admin/users").then(setList), []);
  useEffect(() => {
    load();
  }, [load]);
  return (
    <div>
      <div className="mb-3 flex justify-end"><Button icon={<Plus className="h-4 w-4" />} onClick={() => setOpen(true)}>Add user</Button></div>
      <Card padded={false}>{!list ? <Spinner /> : <Table<U> rows={list} keyFn={(u) => u.id} columns={[{ key: "name", label: "Name" }, { key: "email", label: "Email" }, { key: "role", label: "Role", render: (u) => <Select value={u.role} className="w-28" onChange={(e) => api(`/admin/users/${u.id}`, { method: "PATCH", json: { role: e.target.value } }).then(load)}><option>admin</option><option>analyst</option><option>viewer</option></Select> }, { key: "department", label: "Department", render: (u) => u.department || "—" }, { key: "active", label: "Active", render: (u) => <Toggle checked={u.is_active} onChange={(v) => api(`/admin/users/${u.id}`, { method: "PATCH", json: { is_active: v } }).then(load)} /> }]} />}</Card>
      <p className="mt-3 text-xs text-muted">Roles: <b>admin</b> full access · <b>analyst</b> can edit metadata and query all permitted data · <b>viewer</b> read-only, PII masked, restricted columns hidden.</p>
      <Modal open={open} onClose={() => setOpen(false)} title="Add user">
        <div className="space-y-3">
          <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label="Email"><Input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
          <Field label="Password"><Input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
          <div className="grid grid-cols-2 gap-3"><Field label="Role"><Select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}><option>admin</option><option>analyst</option><option>viewer</option></Select></Field><Field label="Department"><Input value={f.department} onChange={(e) => setF({ ...f, department: e.target.value })} /></Field></div>
          <div className="flex justify-end"><Button onClick={() => api("/admin/users", { method: "POST", json: { ...f, department: f.department || null } }).then(() => { setOpen(false); load(); })}>Create</Button></div>
        </div>
      </Modal>
    </div>
  );
}

function Permissions() {
  const [list, setList] = useState<Perm[] | null>(null);
  const [open, setOpen] = useState(false);
  const [tables, setTables] = useState<{ id: string; table_name: string; source_name: string; dataset_id: string; dataset_name: string }[]>([]);
  const [cols, setCols] = useState<{ id: string; column_name: string }[]>([]);
  const [f, setF] = useState({ role: "viewer", resource_type: "table", resource_id: "", access: "deny", row_filter: "", mask_columns: true, table_for_col: "" });
  const load = useCallback(() => api<Perm[]>("/admin/permissions").then(setList), []);
  useEffect(() => {
    load();
    api<typeof tables>("/catalog/tables").then(setTables);
  }, [load]);
  useEffect(() => {
    if (f.table_for_col) api<{ columns: typeof cols }>(`/catalog/tables/${f.table_for_col}`).then((d) => setCols(d.columns));
  }, [f.table_for_col]);
  const datasets = Array.from(new Map(tables.map((t) => [t.dataset_id, t.dataset_name])).entries());
  return (
    <div>
      <div className="mb-3 flex justify-end"><Button icon={<Plus className="h-4 w-4" />} onClick={() => setOpen(true)}>Add rule</Button></div>
      <Card padded={false}>{!list ? <Spinner /> : <Table<Perm> rows={list} keyFn={(p) => p.id} columns={[{ key: "role", label: "Role", render: (p) => <Badge tone="accent">{p.role}</Badge> }, { key: "resource_type", label: "Scope" }, { key: "resource_name", label: "Resource", render: (p) => <span className="font-mono text-xs">{p.resource_name}</span> }, { key: "access", label: "Access", render: (p) => <Badge tone={p.access === "allow" ? "success" : "danger"}>{p.access}</Badge> }, { key: "row_filter", label: "Row filter (RLS)", render: (p) => <span className="font-mono text-[11px]">{p.row_filter || "—"}</span> }, { key: "mask", label: "PII masking", render: (p) => (p.mask_columns ? "on" : "off") }, { key: "x", label: "", render: (p) => <button className="text-muted hover:text-danger" onClick={() => api(`/admin/permissions/${p.id}`, { method: "DELETE" }).then(load)}><Trash2 className="h-3.5 w-3.5" /></button> }]} empty="No rules: analysts and viewers can see every table (PII masked for non-admins)." />}</Card>
      <p className="mt-3 text-xs text-muted">Once a role has any <b>allow</b> rule, it becomes default-deny for that role. Row filters are SQL predicates injected into every query on the table. Admins bypass all rules; the AI assistant is bound by the same rules as the user asking.</p>
      <Modal open={open} onClose={() => setOpen(false)} title="Add permission rule">
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-3">
            <Field label="Role"><Select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}><option>analyst</option><option>viewer</option></Select></Field>
            <Field label="Scope"><Select value={f.resource_type} onChange={(e) => setF({ ...f, resource_type: e.target.value, resource_id: "" })}><option value="dataset">dataset</option><option value="table">table</option><option value="column">column</option></Select></Field>
            <Field label="Access"><Select value={f.access} onChange={(e) => setF({ ...f, access: e.target.value })}><option>allow</option><option>deny</option></Select></Field>
          </div>
          {f.resource_type === "dataset" && <Field label="Dataset"><Select value={f.resource_id} onChange={(e) => setF({ ...f, resource_id: e.target.value })}><option value="">Select…</option>{datasets.map(([id, n]) => <option key={id} value={id}>{n}</option>)}</Select></Field>}
          {f.resource_type === "table" && <Field label="Table"><Select value={f.resource_id} onChange={(e) => setF({ ...f, resource_id: e.target.value })}><option value="">Select…</option>{tables.map((t) => <option key={t.id} value={t.id}>{t.source_name} · {t.table_name}</option>)}</Select></Field>}
          {f.resource_type === "column" && <div className="grid grid-cols-2 gap-3"><Field label="Table"><Select value={f.table_for_col} onChange={(e) => setF({ ...f, table_for_col: e.target.value })}><option value="">Select…</option>{tables.map((t) => <option key={t.id} value={t.id}>{t.source_name} · {t.table_name}</option>)}</Select></Field><Field label="Column"><Select value={f.resource_id} onChange={(e) => setF({ ...f, resource_id: e.target.value })}><option value="">Select…</option>{cols.map((c) => <option key={c.id} value={c.id}>{c.column_name}</option>)}</Select></Field></div>}
          {f.resource_type === "table" && f.access === "allow" && <><Field label="Row-level filter (SQL predicate)" hint="e.g. department = 'Sales' or region IN ('West','North')"><Input className="font-mono" value={f.row_filter} onChange={(e) => setF({ ...f, row_filter: e.target.value })} /></Field><Toggle checked={f.mask_columns} onChange={(v) => setF({ ...f, mask_columns: v })} label="Mask PII columns for this role" /></>}
          <div className="flex justify-end"><Button disabled={!f.resource_id} onClick={() => api("/admin/permissions", { method: "POST", json: { role: f.role, resource_type: f.resource_type, resource_id: f.resource_id, access: f.access, row_filter: f.row_filter || null, mask_columns: f.mask_columns } }).then(() => { setOpen(false); load(); })}>Add rule</Button></div>
        </div>
      </Modal>
    </div>
  );
}

function Monitoring() {
  const [m, setM] = useState<Mon | null>(null);
  useEffect(() => {
    api<Mon>("/admin/monitoring").then(setM);
    const t = setInterval(() => api<Mon>("/admin/monitoring").then(setM), 10000);
    return () => clearInterval(t);
  }, []);
  if (!m) return <Spinner />;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-6">
        <Stat label="Queries (7d)" value={m.queries.total} sub={Object.entries(m.queries.by_status).map(([k, v]) => `${v} ${k}`).join(" · ")} />
        <Stat label="Query p50 / p95" value={`${m.queries.p50_ms} / ${m.queries.p95_ms} ms`} />
        <Stat label="AI requests (7d)" value={m.ai.requests} sub={`${m.ai.tokens.toLocaleString()} tokens`} />
        <Stat label="AI latency" value={`${m.ai.avg_latency_ms} ms`} sub="average" />
        <Stat label="Jobs (7d)" value={m.jobs.total} sub={`${m.jobs.running} running · ${m.jobs.failed.length} failed`} tone={m.jobs.failed.length ? "warning" : undefined} />
        <Stat label="Sources" value={m.sources.filter((s) => s.status !== "error").length + "/" + m.sources.length} sub="healthy" tone={m.sources.some((s) => s.status === "error") ? "danger" : "success"} />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Recent query errors" padded={false}><Table rows={m.queries.errors} keyFn={(r) => r.ref} dense columns={[{ key: "ref", label: "Ref", render: (r) => <span className="font-mono text-xs">{r.ref}</span> }, { key: "error", label: "Error", render: (r) => <span className="text-xs text-danger">{r.error}</span> }, { key: "when", label: "When", render: (r) => timeAgo(r.created_at) }]} empty="No query errors in the last 7 days." /></Card>
        <Card title="Failed jobs" padded={false}><Table rows={m.jobs.failed} keyFn={(r) => r.id} dense columns={[{ key: "kind", label: "Job" }, { key: "message", label: "Error", render: (r) => <span className="text-xs text-danger">{r.message}</span> }, { key: "when", label: "When", render: (r) => timeAgo(r.created_at) }]} empty="No failed jobs." /></Card>
      </div>
      <Card title="Connector health" padded={false}><Table rows={m.sources} keyFn={(s) => s.name} dense columns={[{ key: "name", label: "Source" }, { key: "status", label: "Status", render: (s) => <Badge tone={statusTone(s.status)}>{s.status}</Badge> }, { key: "err", label: "Last error", render: (s) => <span className="text-xs text-danger">{s.last_error || ""}</span> }]} /></Card>
    </div>
  );
}
