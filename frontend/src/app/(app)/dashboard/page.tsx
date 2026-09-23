"use client";

import { api } from "@/lib/api";
import { Badge, Button, Card, PageHeader, Spinner, Stat, statusTone } from "@/components/ui";
import { Chart } from "@/components/chart";
import { Activity, ArrowRight, Bot, Database, Lightbulb, Network, ShieldAlert, Table2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

type Dash = {
  sources: { total: number; connected: number; error: number; list: { id: string; name: string; type: string; status: string; freshness: string }[] };
  tables: number;
  columns: number;
  metrics: number;
  relationships: { approved: number; suggested: number };
  quality: { open: number; critical: number };
  insights: { id: string; title: string; summary: string; severity: string; created_at: string; details: { factors: { statement: string; strength: string }[]; change_pct: number | null } }[];
  queries_24h: number;
  ai_requests_24h: number;
  domains: { domain: string; tables: number }[];
};

export default function Dashboard() {
  const [d, setD] = useState<Dash | null>(null);
  useEffect(() => {
    api<Dash>("/admin/dashboard").then(setD);
  }, []);
  if (!d) return <Spinner />;
  return (
    <div>
      <PageHeader title="Dashboard" description="Health of the organisation's data ecosystem at a glance." actions={<Link href="/assistant"><Button icon={<Bot className="h-4 w-4" />}>Ask the assistant</Button></Link>} />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Data sources" value={d.sources.total} sub={`${d.sources.connected} healthy · ${d.sources.error} errors`} icon={<Database className="h-4 w-4" />} tone={d.sources.error ? "warning" : undefined} />
        <Stat label="Tables catalogued" value={d.tables} sub={`${d.columns.toLocaleString()} columns profiled`} icon={<Table2 className="h-4 w-4" />} />
        <Stat label="Governed metrics" value={d.metrics} sub="business definitions" icon={<Activity className="h-4 w-4" />} />
        <Stat label="Relationships" value={d.relationships.approved} sub={`${d.relationships.suggested} awaiting review`} icon={<Network className="h-4 w-4" />} tone={d.relationships.suggested ? "warning" : undefined} />
        <Stat label="Open quality issues" value={d.quality.open} sub={`${d.quality.critical} critical`} icon={<ShieldAlert className="h-4 w-4" />} tone={d.quality.critical ? "danger" : d.quality.open ? "warning" : "success"} />
        <Stat label="Last 24h" value={d.queries_24h} sub={`queries · ${d.ai_requests_24h} AI requests`} icon={<Bot className="h-4 w-4" />} />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <Card title="Proactive insights" subtitle="Anomalies detected across governed metrics" className="lg:col-span-2" actions={<Link href="/insights" className="text-xs text-primary hover:underline">View all</Link>} padded={false}>
          {d.insights.length === 0 && <p className="p-5 text-sm text-muted">No new insights. Run the insight engine from the Insights page.</p>}
          <ul className="divide-y">
            {d.insights.map((i) => (
              <li key={i.id} className="flex items-start gap-3 px-5 py-3.5">
                <div className={`mt-0.5 rounded-full p-1.5 ${i.severity === "critical" ? "bg-danger/12 text-danger" : i.severity === "warning" ? "bg-warning/15 text-warning" : "bg-primary/12 text-primary"}`}>
                  <Lightbulb className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{i.title}</p>
                  <p className="mt-0.5 text-xs text-muted">{i.summary}</p>
                  {i.details?.factors?.length > 0 && (
                    <ul className="mt-2 space-y-0.5">
                      {i.details.factors.slice(0, 3).map((f, k) => (
                        <li key={k} className="flex items-start gap-1.5 text-xs">
                          <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-muted" />
                          <span className="text-text/80">{f.statement}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <Link href={`/assistant?q=${encodeURIComponent("Why did " + i.title.toLowerCase().replace(/ (declined|increased).*/, "") + " change in " + (i.title.match(/in (.*)$/)?.[1] || "the latest period") + "?")}`}>
                  <Button size="sm" variant="outline" icon={<ArrowRight className="h-3.5 w-3.5" />}>Investigate</Button>
                </Link>
              </li>
            ))}
          </ul>
        </Card>
        <div className="space-y-6">
          <Card title="Data sources" padded={false} actions={<Link href="/sources" className="text-xs text-primary hover:underline">Manage</Link>}>
            <ul className="divide-y">
              {d.sources.list.map((s) => (
                <li key={s.id} className="flex items-center justify-between px-5 py-2.5 text-sm">
                  <Link href={`/sources/${s.id}`} className="min-w-0 hover:underline">
                    <p className="truncate font-medium">{s.name}</p>
                    <p className="text-[11px] text-muted">{s.type} · {s.freshness}</p>
                  </Link>
                  <Badge tone={statusTone(s.status)}>{s.status}</Badge>
                </li>
              ))}
            </ul>
          </Card>
          <Card title="Coverage by domain">
            <Chart spec={{ type: "bar", x: d.domains.map((x) => x.domain), series: [{ name: "tables", data: d.domains.map((x) => x.tables) }] }} height={200} />
          </Card>
        </div>
      </div>
    </div>
  );
}
