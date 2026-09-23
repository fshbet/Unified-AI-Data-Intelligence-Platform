"use client";

import { api, timeAgo } from "@/lib/api";
import { Badge, Button, Card, Empty, Field, Input, Modal, PageHeader, Select, Spinner, Table, Textarea, Toggle, statusTone } from "@/components/ui";
import { Database, Plus, RefreshCw, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

export type Source = { id: string; name: string; type: string; config: Record<string, unknown>; owner?: string | null; department?: string | null; description?: string | null; refresh_frequency: string; status: string; is_enabled: boolean; read_only: boolean; last_sync_at?: string | null; last_error?: string | null; health: Record<string, unknown>; table_count: number; freshness: string };
export type ConnectorType = { type: string; display_name: string; category: string; dialect: string; fields: { name: string; label: string; type: string; required: boolean; default?: unknown; options?: string[]; help?: string }[] };

const FREQ = ["manual", "15m", "hourly", "daily", "weekly"];

export function SourceForm({ types, initial, onSaved, onClose }: { types: ConnectorType[]; initial?: Source | null; onSaved: (s: Source) => void; onClose: () => void }) {
  const [type, setType] = useState(initial?.type || "postgresql");
  const [name, setName] = useState(initial?.name || "");
  const [cfg, setCfg] = useState<Record<string, unknown>>(initial?.config || {});
  const [meta, setMeta] = useState({ owner: initial?.owner || "", department: initial?.department || "", description: initial?.description || "", refresh_frequency: initial?.refresh_frequency || "manual", read_only: initial?.read_only ?? true, is_enabled: initial?.is_enabled ?? true });
  const [files, setFiles] = useState<FileList | null>(null);
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const ct = types.find((t) => t.type === type);

  useEffect(() => {
    if (!initial && ct) {
      const defaults: Record<string, unknown> = {};
      ct.fields.forEach((f) => f.default !== undefined && f.default !== null && (defaults[f.name] = f.default));
      setCfg(defaults);
    }
  }, [type, ct, initial]);

  const body = { name, type, config: cfg, ...meta };
  const doTest = async () => {
    setBusy("test");
    setErr(null);
    try {
      setTest(initial ? await api(`/sources/${initial.id}/test`, { method: "POST" }) : await api("/sources/test", { method: "POST", json: body }));
    } catch (e) {
      setTest({ ok: false, message: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };
  const save = async () => {
    setBusy("save");
    setErr(null);
    try {
      let s: Source;
      if (type === "file" && !initial) {
        const fd = new FormData();
        fd.append("name", name);
        if (meta.department) fd.append("department", meta.department);
        if (meta.description) fd.append("description", meta.description);
        Array.from(files || []).forEach((f) => fd.append("files", f));
        await api("/sources/upload-new", { method: "POST", body: fd });
        const all = await api<Source[]>("/sources");
        s = all.find((x) => x.name === name)!;
      } else {
        s = initial ? await api<Source>(`/sources/${initial.id}`, { method: "PATCH", json: body }) : await api<Source>("/sources", { method: "POST", json: body });
        if (type === "file" && files?.length) {
          const fd = new FormData();
          Array.from(files).forEach((f) => fd.append("files", f));
          await api(`/sources/${s.id}/upload`, { method: "POST", body: fd });
        } else if (!initial) {
          await api(`/sources/${s.id}/sync`, { method: "POST" });
        }
      }
      onSaved(s);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Corporate DB" />
        </Field>
        <Field label="Type">
          <Select value={type} onChange={(e) => setType(e.target.value)} disabled={!!initial}>
            {["database", "file", "nosql", "bi", "api"].map((cat) => (
              <optgroup key={cat} label={cat === "bi" ? "BI semantic models" : cat[0].toUpperCase() + cat.slice(1)}>
                {types.filter((t) => t.category === cat).map((t) => (
                  <option key={t.type} value={t.type}>{t.display_name}</option>
                ))}
              </optgroup>
            ))}
          </Select>
        </Field>
      </div>
      {ct && (
        <div className="rounded-lg border bg-surface-2/50 p-3">
          <p className="mb-2 text-xs font-medium text-muted">Connection</p>
          <div className="grid grid-cols-2 gap-3">
            {ct.fields.map((f) =>
              f.type === "file" ? (
                <Field key={f.name} label={f.label} className="col-span-2" hint="CSV, TSV, Excel, JSON, JSONL, Parquet, XML. Multiple files allowed; Excel sheets become separate tables.">
                  <input type="file" multiple accept=".csv,.tsv,.txt,.xlsx,.xls,.xlsm,.json,.jsonl,.ndjson,.parquet,.xml" onChange={(e) => setFiles(e.target.files)} className="block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white" />
                  {initial && Array.isArray(initial.config.files) && <p className="mt-1 text-[11px] text-muted">Current: {(initial.config.files as string[]).map((p) => p.split(/[\\/]/).pop()).join(", ")}</p>}
                </Field>
              ) : f.type === "boolean" ? (
                <div key={f.name} className="flex items-end pb-2">
                  <Toggle checked={!!cfg[f.name]} onChange={(v) => setCfg({ ...cfg, [f.name]: v })} label={f.label} />
                </div>
              ) : f.type === "select" ? (
                <Field key={f.name} label={f.label} hint={f.help}>
                  <Select value={String(cfg[f.name] ?? f.default ?? "")} onChange={(e) => setCfg({ ...cfg, [f.name]: e.target.value })}>
                    {f.options?.map((o) => <option key={o}>{o}</option>)}
                  </Select>
                </Field>
              ) : f.type === "textarea" ? (
                <Field key={f.name} label={f.label} hint={f.help} className="col-span-2">
                  <Textarea value={String(cfg[f.name] ?? "")} onChange={(e) => setCfg({ ...cfg, [f.name]: e.target.value })} />
                </Field>
              ) : (
                <Field key={f.name} label={f.label + (f.required ? "" : " (optional)")} hint={f.help}>
                  <Input type={f.type === "password" ? "password" : f.type === "integer" ? "number" : "text"} value={String(cfg[f.name] ?? "")} onChange={(e) => setCfg({ ...cfg, [f.name]: f.type === "integer" ? Number(e.target.value) : e.target.value })} placeholder={f.type === "password" && initial ? "unchanged" : undefined} />
                </Field>
              ),
            )}
          </div>
        </div>
      )}
      <div className="grid grid-cols-3 gap-3">
        <Field label="Owner"><Input value={meta.owner} onChange={(e) => setMeta({ ...meta, owner: e.target.value })} placeholder="owner@company.com" /></Field>
        <Field label="Department"><Input value={meta.department} onChange={(e) => setMeta({ ...meta, department: e.target.value })} placeholder="Finance" /></Field>
        <Field label="Refresh">
          <Select value={meta.refresh_frequency} onChange={(e) => setMeta({ ...meta, refresh_frequency: e.target.value })}>{FREQ.map((f) => <option key={f}>{f}</option>)}</Select>
        </Field>
      </div>
      <Field label="Business description"><Textarea value={meta.description} onChange={(e) => setMeta({ ...meta, description: e.target.value })} placeholder="What this system is, who owns it, what it is used for…" /></Field>
      <div className="flex gap-6">
        <Toggle checked={meta.read_only} onChange={(v) => setMeta({ ...meta, read_only: v })} label="Read-only access" />
        <Toggle checked={meta.is_enabled} onChange={(v) => setMeta({ ...meta, is_enabled: v })} label="Enabled" />
      </div>
      {test && <p className={`rounded-lg px-3 py-2 text-xs ${test.ok ? "bg-success/10 text-success" : "bg-danger/10 text-danger"}`}>{test.ok ? "✓ " : "✗ "}{test.message}</p>}
      {err && <p className="rounded-lg bg-danger/10 px-3 py-2 text-xs text-danger">{err}</p>}
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        {type !== "file" && <Button variant="outline" onClick={doTest} loading={busy === "test"}>Test connection</Button>}
        <Button onClick={save} loading={busy === "save"} disabled={!name}>{initial ? "Save changes" : "Add & sync"}</Button>
      </div>
    </div>
  );
}

export default function SourcesPage() {
  const [sources, setSources] = useState<Source[] | null>(null);
  const [types, setTypes] = useState<ConnectorType[]>([]);
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const load = useCallback(() => api<Source[]>("/sources").then(setSources), []);
  useEffect(() => {
    load();
    api<ConnectorType[]>("/sources/types").then(setTypes);
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div>
      <PageHeader title="Data Sources" description="Connect databases, files, NoSQL stores, APIs and BI semantic models. Every source is discovered, profiled and added to the semantic catalog." actions={<Button icon={<Plus className="h-4 w-4" />} onClick={() => setOpen(true)}>Add data source</Button>} />
      {!sources ? (
        <Spinner />
      ) : sources.length === 0 ? (
        <Empty icon={<Database className="h-6 w-6" />} title="No data sources yet" description="Connect a database or upload files to start building the organisational data context." action={<Button icon={<Upload className="h-4 w-4" />} onClick={() => setOpen(true)}>Add your first source</Button>} />
      ) : (
        <Card padded={false}>
          <Table<Source>
            rows={sources}
            keyFn={(s) => s.id}
            onRowClick={(s) => router.push(`/sources/${s.id}`)}
            columns={[
              { key: "name", label: "Name", render: (s) => <div><p className="font-medium">{s.name}</p><p className="text-[11px] text-muted">{s.description?.slice(0, 80)}</p></div> },
              { key: "type", label: "Type", render: (s) => <Badge>{types.find((t) => t.type === s.type)?.display_name || s.type}</Badge> },
              { key: "status", label: "Status", render: (s) => <Badge tone={statusTone(s.is_enabled ? s.status : "disabled")}>{s.is_enabled ? s.status : "disabled"}</Badge> },
              { key: "tables", label: "Tables", render: (s) => s.table_count, align: "right" },
              { key: "sync", label: "Last sync", render: (s) => <span title={s.last_sync_at || ""}>{s.last_sync_at ? timeAgo(s.last_sync_at) : "never"}</span> },
              { key: "freq", label: "Refresh", render: (s) => s.refresh_frequency },
              { key: "owner", label: "Owner", render: (s) => s.owner || "—" },
              { key: "actions", label: "", render: (s) => <Button size="sm" variant="ghost" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={(e) => { e.stopPropagation(); api(`/sources/${s.id}/sync`, { method: "POST" }).then(load); }}>Sync</Button> },
            ]}
          />
        </Card>
      )}
      <Modal open={open} onClose={() => setOpen(false)} title="Add data source" width="max-w-2xl">
        <SourceForm types={types} onSaved={(s) => { setOpen(false); router.push(`/sources/${s.id}`); }} onClose={() => setOpen(false)} />
      </Modal>
    </div>
  );
}
