"use client";

import { api, timeAgo } from "@/lib/api";
import { QueryModal } from "@/components/answer";
import { Badge, Card, Input, PageHeader, Select, Spinner, Table, statusTone } from "@/components/ui";
import { useEffect, useState } from "react";

type Q = { id: string; ref: string; user?: string | null; source?: string | null; sql: string; purpose?: string | null; status: string; row_count?: number | null; duration_ms?: number | null; error?: string | null; created_at: string; conversation_id?: string | null };

export default function HistoryPage() {
  const [rows, setRows] = useState<Q[] | null>(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [ref, setRef] = useState<string | null>(null);
  useEffect(() => {
    const t = setTimeout(() => api<Q[]>(`/admin/queries?limit=300${q ? `&q=${encodeURIComponent(q)}` : ""}${status ? `&status=${status}` : ""}`).then(setRows), 200);
    return () => clearTimeout(t);
  }, [q, status]);
  return (
    <div>
      <PageHeader title="Query History" description="Every SQL statement generated or executed on your behalf, with status, timing and results." actions={<><Input className="w-64" placeholder="Search SQL / purpose…" value={q} onChange={(e) => setQ(e.target.value)} /><Select className="w-32" value={status} onChange={(e) => setStatus(e.target.value)}><option value="">All</option><option value="ok">ok</option><option value="error">error</option><option value="blocked">blocked</option></Select></>} />
      <Card padded={false}>
        {!rows ? <Spinner /> : (
          <Table<Q>
            rows={rows}
            keyFn={(r) => r.id}
            dense
            onRowClick={(r) => setRef(r.ref)}
            columns={[
              { key: "ref", label: "Ref", render: (r) => <span className="qref">{r.ref}</span> },
              { key: "when", label: "When", render: (r) => <span className="text-xs text-muted whitespace-nowrap">{timeAgo(r.created_at)}</span> },
              { key: "user", label: "User", render: (r) => <span className="text-xs">{r.user}</span> },
              { key: "source", label: "Source", render: (r) => <Badge tone="info">{r.source}</Badge> },
              { key: "purpose", label: "Purpose", render: (r) => <span className="text-xs text-muted">{r.purpose || "—"}</span> },
              { key: "sql", label: "SQL", render: (r) => <span className="line-clamp-1 max-w-md font-mono text-[11px]">{r.sql}</span> },
              { key: "status", label: "Status", render: (r) => <Badge tone={statusTone(r.status)}>{r.status}</Badge> },
              { key: "rows", label: "Rows", align: "right", render: (r) => r.row_count ?? "—" },
              { key: "ms", label: "ms", align: "right", render: (r) => r.duration_ms ?? "—" },
            ]}
          />
        )}
      </Card>
      <QueryModal refId={ref} onClose={() => setRef(null)} />
    </div>
  );
}
