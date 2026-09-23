"use client";

import { api } from "@/lib/api";
import { Badge, Card, Input, PageHeader, Select, Spinner, Table } from "@/components/ui";
import { Search } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

type Dataset = { id: string; source_id: string; name: string; description?: string | null; business_domain?: string | null; owner?: string | null; sensitivity: string; source_name: string; source_type: string; table_count: number };
type TableRow = { id: string; dataset_id: string; table_name: string; schema_name?: string | null; business_name?: string | null; description?: string | null; business_domain?: string | null; row_count?: number | null; column_count?: number | null; open_issues: number; relationship_count: number; source_name: string; source_id: string; dataset_name: string; date_column?: string | null };

function DatasetsInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [tables, setTables] = useState<TableRow[]>([]);
  const [q, setQ] = useState("");
  const [source, setSource] = useState(params.get("source") || "");
  const [domain, setDomain] = useState("");
  useEffect(() => {
    api<Dataset[]>("/catalog/datasets").then(setDatasets);
    api<TableRow[]>("/catalog/tables").then(setTables);
  }, []);
  const domains = useMemo(() => Array.from(new Set(tables.map((t) => t.business_domain).filter(Boolean))) as string[], [tables]);
  const filtered = tables.filter((t) => (!source || t.source_id === source) && (!domain || t.business_domain === domain) && (!q || `${t.table_name} ${t.business_name} ${t.description}`.toLowerCase().includes(q.toLowerCase())));
  if (!datasets) return <Spinner />;
  return (
    <div>
      <PageHeader title="Datasets" description="Source → Database → Schema → Table → Column. Browse everything the platform knows about your data." />
      <div className="mb-4 grid gap-3 md:grid-cols-4">
        {datasets.map((d) => (
          <button key={d.id} onClick={() => setSource(source === d.source_id ? "" : d.source_id)} className={`rounded-xl border p-3 text-left transition-colors ${source === d.source_id ? "border-primary bg-primary/5" : "bg-surface hover:bg-surface-2"}`}>
            <div className="flex items-center justify-between"><p className="text-sm font-semibold">{d.name}</p><Badge tone={d.sensitivity === "restricted" ? "danger" : d.sensitivity === "sensitive" ? "warning" : "neutral"}>{d.sensitivity}</Badge></div>
            <p className="mt-0.5 line-clamp-2 text-[11px] text-muted">{d.description || "No description"}</p>
            <p className="mt-2 text-[11px] text-muted">{d.source_type} · {d.table_count} tables · {d.business_domain || "domain unassigned"}</p>
          </button>
        ))}
      </div>
      <Card padded={false} title="Tables" actions={
        <>
          <div className="relative"><Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted" /><Input className="w-56 pl-8" placeholder="Search tables…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
          <Select className="w-40" value={domain} onChange={(e) => setDomain(e.target.value)}><option value="">All domains</option>{domains.map((d) => <option key={d}>{d}</option>)}</Select>
        </>
      }>
        <Table<TableRow>
          rows={filtered}
          keyFn={(t) => t.id}
          onRowClick={(t) => router.push(`/tables/${t.id}`)}
          columns={[
            { key: "name", label: "Table", render: (t) => <div><p className="font-mono text-xs font-medium">{t.schema_name ? `${t.schema_name}.` : ""}{t.table_name}</p><p className="text-[11px] text-muted">{t.business_name}</p></div> },
            { key: "source", label: "Source", render: (t) => <span className="text-xs">{t.source_name}</span> },
            { key: "domain", label: "Domain", render: (t) => t.business_domain ? <Badge tone="accent">{t.business_domain}</Badge> : "—" },
            { key: "desc", label: "Description", render: (t) => <span className="text-xs text-muted line-clamp-2">{t.description || "—"}</span> },
            { key: "date", label: "Time axis", render: (t) => <span className="font-mono text-[11px]">{t.date_column || "—"}</span> },
            { key: "rows", label: "Rows", align: "right", render: (t) => t.row_count?.toLocaleString() ?? "—" },
            { key: "cols", label: "Cols", align: "right", render: (t) => t.column_count ?? "—" },
            { key: "q", label: "Quality", render: (t) => (t.open_issues ? <Badge tone="warning">{t.open_issues}</Badge> : <Badge tone="success">ok</Badge>) },
          ]}
        />
      </Card>
    </div>
  );
}

export default function DatasetsPage() {
  return <Suspense fallback={<Spinner />}><DatasetsInner /></Suspense>;
}
