"use client";

import { api, timeAgo } from "@/lib/api";
import { Badge, Button, Card, Modal, PageHeader, Spinner, Stat, Table, statusTone } from "@/components/ui";
import { ConnectorType, Source, SourceForm } from "../page";
import { useAuth } from "@/lib/auth";
import { CheckCircle2, Pencil, RefreshCw, Trash2, XCircle } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

type TableRow = { id: string; table_name: string; schema_name?: string | null; business_name?: string | null; description?: string | null; business_domain?: string | null; row_count?: number | null; column_count?: number | null; open_issues: number; relationship_count: number; last_profiled_at?: string | null };
type Job = { id: string; kind: string; status: string; progress: number; message?: string | null; result: Record<string, unknown>; created_at: string; finished_at?: string | null };

export default function SourceDetail() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { user } = useAuth();
  const [s, setS] = useState<Source | null>(null);
  const [tables, setTables] = useState<TableRow[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [types, setTypes] = useState<ConnectorType[]>([]);
  const [edit, setEdit] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; message: string; latency_ms?: number } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [src, tbl, jb] = await Promise.all([api<Source>(`/sources/${id}`), api<TableRow[]>(`/catalog/tables?source_id=${id}`), api<Job[]>(`/sources/${id}/jobs`)]);
    setS(src);
    setTables(tbl);
    setJobs(jb);
  }, [id]);
  useEffect(() => {
    load();
    api<ConnectorType[]>("/sources/types").then(setTypes);
  }, [load]);
  useEffect(() => {
    if (jobs.some((j) => j.status === "running" || j.status === "queued")) {
      const t = setTimeout(load, 2500);
      return () => clearTimeout(t);
    }
  }, [jobs, load]);

  if (!s) return <Spinner />;
  const running = jobs.find((j) => j.status === "running" || j.status === "queued");
  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    try {
      await fn();
      await load();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <PageHeader
        title={s.name}
        description={s.description || `${types.find((t) => t.type === s.type)?.display_name || s.type} source`}
        actions={
          <>
            <Button variant="outline" loading={busy === "test"} onClick={() => act("test", async () => setTest(await api(`/sources/${id}/test`, { method: "POST" })))}>Test connection</Button>
            <Button variant="outline" icon={<RefreshCw className="h-4 w-4" />} loading={busy === "sync"} onClick={() => act("sync", () => api(`/sources/${id}/sync`, { method: "POST" }))}>Refresh metadata</Button>
            <Button variant="outline" icon={<Pencil className="h-4 w-4" />} onClick={() => setEdit(true)}>Edit</Button>
            <Button variant="outline" onClick={() => act("toggle", () => api(`/sources/${id}`, { method: "PATCH", json: { is_enabled: !s.is_enabled } }))}>{s.is_enabled ? "Disable" : "Enable"}</Button>
            {user?.role === "admin" && <Button variant="danger" icon={<Trash2 className="h-4 w-4" />} onClick={() => confirm(`Delete source "${s.name}" and all its metadata?`) && api(`/sources/${id}`, { method: "DELETE" }).then(() => router.push("/sources"))}>Delete</Button>}
          </>
        }
      />
      {test && (
        <div className={`mb-4 flex items-center gap-2 rounded-lg px-3 py-2 text-sm ${test.ok ? "bg-success/10 text-success" : "bg-danger/10 text-danger"}`}>
          {test.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />} {test.message} {test.latency_ms !== undefined && `(${test.latency_ms} ms)`}
        </div>
      )}
      {running && (
        <div className="mb-4 rounded-lg border bg-primary/5 px-4 py-3 text-sm">
          <div className="flex items-center justify-between"><span className="font-medium">{running.kind.replace("_", " ")} · {running.message || running.status}</span><span className="text-muted">{running.progress}%</span></div>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-2"><div className="h-full bg-primary transition-all" style={{ width: `${running.progress}%` }} /></div>
        </div>
      )}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="Status" value={<Badge tone={statusTone(s.is_enabled ? s.status : "disabled")} className="text-sm">{s.is_enabled ? s.status : "disabled"}</Badge>} sub={s.last_error ? <span className="text-danger">{s.last_error.slice(0, 80)}</span> : "healthy"} />
        <Stat label="Last sync" value={s.last_sync_at ? timeAgo(s.last_sync_at) : "never"} sub={`refresh: ${s.refresh_frequency}`} />
        <Stat label="Tables" value={tables.length} sub={`${tables.reduce((a, t) => a + (t.column_count || 0), 0)} columns`} />
        <Stat label="Rows" value={tables.reduce((a, t) => a + (t.row_count || 0), 0).toLocaleString()} sub="across all tables" />
        <Stat label="Access" value={s.read_only ? "Read-only" : "Read/write"} sub={s.owner ? `Owner: ${s.owner}` : "no owner assigned"} />
      </div>
      <Card className="mt-6" title="Tables" subtitle="Click a table to explore columns, statistics, sample data, relationships and quality." padded={false}>
        <Table<TableRow>
          rows={tables}
          keyFn={(t) => t.id}
          onRowClick={(t) => router.push(`/tables/${t.id}`)}
          columns={[
            { key: "name", label: "Table", render: (t) => <div><p className="font-medium font-mono text-xs">{t.schema_name ? `${t.schema_name}.` : ""}{t.table_name}</p><p className="text-[11px] text-muted">{t.business_name}</p></div> },
            { key: "desc", label: "Description", render: (t) => <span className="text-xs text-muted">{t.description?.slice(0, 100) || "—"}</span> },
            { key: "domain", label: "Domain", render: (t) => t.business_domain ? <Badge tone="accent">{t.business_domain}</Badge> : "—" },
            { key: "rows", label: "Rows", align: "right", render: (t) => t.row_count?.toLocaleString() ?? "—" },
            { key: "cols", label: "Columns", align: "right", render: (t) => t.column_count ?? "—" },
            { key: "rel", label: "Relationships", align: "right", render: (t) => t.relationship_count },
            { key: "q", label: "Quality", render: (t) => (t.open_issues ? <Badge tone="warning">{t.open_issues} issues</Badge> : <Badge tone="success">ok</Badge>) },
          ]}
          empty={<span>No tables discovered yet. <button className="text-primary underline" onClick={() => act("sync", () => api(`/sources/${id}/sync`, { method: "POST" }))}>Run a sync</button>.</span>}
        />
      </Card>
      <Card className="mt-6" title="Sync history" padded={false}>
        <Table<Job> rows={jobs} keyFn={(j) => j.id} dense columns={[{ key: "kind", label: "Job", render: (j) => j.kind.replace("_", " ") }, { key: "status", label: "Status", render: (j) => <Badge tone={statusTone(j.status)}>{j.status}</Badge> }, { key: "msg", label: "Result", render: (j) => <span className="text-xs text-muted">{j.status === "error" ? j.message : j.result?.tables !== undefined ? `${j.result.tables} tables, ${j.result.columns} columns, ${j.result.relationships_suggested ?? 0} relationships suggested` : j.message || "—"}</span> }, { key: "when", label: "When", render: (j) => timeAgo(j.created_at) }]} />
      </Card>
      <p className="mt-4 text-xs text-muted">Explore the semantic catalog for this source in <Link href={`/datasets?source=${id}`} className="text-primary hover:underline">Datasets</Link>.</p>
      <Modal open={edit} onClose={() => setEdit(false)} title={`Edit ${s.name}`} width="max-w-2xl">
        <SourceForm types={types} initial={s} onSaved={() => { setEdit(false); load(); }} onClose={() => setEdit(false)} />
      </Modal>
    </div>
  );
}
