"use client";

import { api, parseDate } from "@/lib/api";
import { Badge, Card, Input, PageHeader, Spinner, Table } from "@/components/ui";
import { useEffect, useState } from "react";

type A = { id: string; user_email?: string | null; action: string; resource_type?: string | null; resource_id?: string | null; details: Record<string, unknown>; ip?: string | null; created_at: string };

export default function AuditPage() {
  const [rows, setRows] = useState<A[] | null>(null);
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  useEffect(() => {
    const t = setTimeout(() => api<A[]>(`/admin/audit?limit=400${q ? `&q=${encodeURIComponent(q)}` : ""}${action ? `&action=${action}` : ""}`).then(setRows), 200);
    return () => clearTimeout(t);
  }, [q, action]);
  return (
    <div>
      <PageHeader title="Audit Logs" description="Who did what, when: logins, source changes, metadata edits, permission changes, questions asked and queries executed." actions={<><Input className="w-64" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} /><Input className="w-44" placeholder="Action prefix (e.g. source.)" value={action} onChange={(e) => setAction(e.target.value)} /></>} />
      <Card padded={false}>
        {!rows ? <Spinner /> : (
          <Table<A> rows={rows} keyFn={(r) => r.id} dense columns={[
            { key: "when", label: "Timestamp", render: (r) => <span className="whitespace-nowrap text-xs">{parseDate(r.created_at).toLocaleString()}</span> },
            { key: "user", label: "User", render: (r) => <span className="text-xs">{r.user_email || "system"}</span> },
            { key: "action", label: "Action", render: (r) => <Badge tone={r.action.includes("delete") ? "danger" : r.action.includes("create") || r.action.includes("approve") ? "success" : "neutral"}>{r.action}</Badge> },
            { key: "res", label: "Resource", render: (r) => <span className="font-mono text-[11px] text-muted">{r.resource_type} {r.resource_id?.slice(0, 8)}</span> },
            { key: "details", label: "Details", render: (r) => <span className="line-clamp-2 max-w-lg font-mono text-[11px] text-muted">{JSON.stringify(r.details)}</span> },
            { key: "ip", label: "IP", render: (r) => <span className="text-xs text-muted">{r.ip || "—"}</span> },
          ]} />
        )}
      </Card>
    </div>
  );
}
