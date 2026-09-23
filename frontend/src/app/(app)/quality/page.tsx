"use client";

import { api, timeAgo } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Card, Empty, PageHeader, Select, Spinner, Stat, statusTone } from "@/components/ui";
import { AlertTriangle, RefreshCw, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

type Issue = { id: string; table_id?: string | null; table?: string | null; source?: string | null; rule: string; severity: string; message: string; details: Record<string, unknown>; status: string; detected_at: string };

export default function QualityPage() {
  const { user } = useAuth();
  const [issues, setIssues] = useState<Issue[] | null>(null);
  const [status, setStatus] = useState("open");
  const [sev, setSev] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<Issue[]>(`/admin/quality?status=${status}${sev ? `&severity=${sev}` : ""}`).then(setIssues), [status, sev]);
  useEffect(() => {
    load();
  }, [load]);
  const all = issues || [];
  const counts = { critical: all.filter((i) => i.severity === "critical").length, warning: all.filter((i) => i.severity === "warning").length, info: all.filter((i) => i.severity === "info").length };
  return (
    <div>
      <PageHeader title="Data Quality" description="Missing values, duplicates, invalid values, schema drift, volume changes, freshness and broken relationships. The AI factors open issues into its confidence." actions={user?.role !== "viewer" && <Button variant="outline" icon={<RefreshCw className="h-4 w-4" />} loading={busy} onClick={async () => { setBusy(true); await api("/admin/quality/run", { method: "POST" }); await load(); setBusy(false); }}>Re-run checks</Button>} />
      <div className="mb-6 grid grid-cols-3 gap-4">
        <Stat label="Critical" value={counts.critical} tone={counts.critical ? "danger" : "success"} />
        <Stat label="Warnings" value={counts.warning} tone={counts.warning ? "warning" : undefined} />
        <Stat label="Informational" value={counts.info} />
      </div>
      <Card padded={false} title="Issues" actions={<><Select className="w-36" value={status} onChange={(e) => setStatus(e.target.value)}><option value="open">Open</option><option value="acknowledged">Acknowledged</option><option value="resolved">Resolved</option><option value="">All</option></Select><Select className="w-36" value={sev} onChange={(e) => setSev(e.target.value)}><option value="">All severities</option><option>critical</option><option>warning</option><option>info</option></Select></>}>
        {!issues ? <Spinner /> : issues.length === 0 ? <div className="p-6"><Empty icon={<ShieldCheck className="h-6 w-6" />} title="No issues" description="All profiled tables pass the configured quality rules." /></div> : (
          <ul className="divide-y">
            {issues.map((i) => (
              <li key={i.id} className="flex items-start gap-3 px-5 py-3.5">
                <div className={`mt-0.5 rounded-full p-1.5 ${i.severity === "critical" ? "bg-danger/12 text-danger" : i.severity === "warning" ? "bg-warning/15 text-warning" : "bg-primary/12 text-primary"}`}><AlertTriangle className="h-4 w-4" /></div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2"><Badge tone={statusTone(i.severity)}>{i.severity}</Badge><span className="text-xs font-medium">{i.rule.replace(/_/g, " ")}</span>{i.table && <Link href={`/tables/${i.table_id}`} className="font-mono text-xs text-primary hover:underline">{i.table}</Link>}<span className="text-[11px] text-muted">{i.source}</span></div>
                  <p className="mt-1 text-sm">{i.message}</p>
                  <p className="mt-0.5 text-[11px] text-muted">Detected {timeAgo(i.detected_at)} · Potential impact: analyses on this table may be incomplete or biased.</p>
                </div>
                {user?.role !== "viewer" && <div className="flex gap-1">{i.status === "open" && <Button size="sm" variant="ghost" onClick={() => api(`/admin/quality/${i.id}`, { method: "PATCH", json: { status: "acknowledged" } }).then(load)}>Acknowledge</Button>}{i.status !== "resolved" && <Button size="sm" variant="outline" onClick={() => api(`/admin/quality/${i.id}`, { method: "PATCH", json: { status: "resolved" } }).then(load)}>Resolve</Button>}</div>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
