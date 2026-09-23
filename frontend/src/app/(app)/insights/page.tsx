"use client";

import { api, fmtPct, timeAgo } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Card, Empty, PageHeader, Spinner, statusTone } from "@/components/ui";
import { ArrowRight, Lightbulb, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

type Insight = { id: string; title: string; summary: string; severity: string; kind: string; period?: string | null; status: string; created_at: string; details: { change_pct?: number | null; before?: number; after?: number; factors?: { statement: string; strength: string; domain?: string | null; query_refs: string[] }[]; top_segments?: { dimension: string; segment: string; change_pct: number | null; contribution_pct: number | null }[]; related?: { metric: string; change_pct: number | null; domain?: string | null }[]; sources?: string[] } };

export default function InsightsPage() {
  const { user } = useAuth();
  const [items, setItems] = useState<Insight[] | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<Insight[]>("/assistant/insights").then(setItems), []);
  useEffect(() => {
    load();
  }, [load]);
  const generate = async () => {
    setBusy(true);
    try {
      const { job_id } = await api<{ job_id: string }>("/assistant/insights/generate", { method: "POST" });
      for (let i = 0; i < 90; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const j = await api<{ status: string }>(`/admin/jobs/${job_id}`);
        if (j.status === "done" || j.status === "error") break;
      }
      await load();
    } finally {
      setBusy(false);
    }
  };
  const visible = (items || []).filter((i) => i.status !== "dismissed");
  return (
    <div>
      <PageHeader title="Insights" description="Proactively detected anomalies, shocks and data-quality risks across every governed metric — each pre-investigated across connected domains." actions={user?.role !== "viewer" && <Button icon={<RefreshCw className="h-4 w-4" />} loading={busy} onClick={generate}>Run insight engine</Button>} />
      {!items ? <Spinner /> : visible.length === 0 ? <Empty icon={<Lightbulb className="h-6 w-6" />} title="No insights yet" description="Run the insight engine to scan every metric's monthly series for anomalies." action={user?.role !== "viewer" && <Button loading={busy} onClick={generate}>Run insight engine</Button>} /> : (
        <div className="grid gap-4 lg:grid-cols-2">
          {visible.map((i) => (
            <Card key={i.id} title={<span className="flex items-center gap-2"><Badge tone={statusTone(i.severity)}>{i.severity}</Badge>{i.title}</span>} subtitle={`${i.kind} · ${timeAgo(i.created_at)}`} actions={<button className="text-xs text-muted hover:text-text" onClick={() => api(`/assistant/insights/${i.id}`, { method: "PATCH", json: { status: "dismissed" } }).then(load)}>Dismiss</button>}>
              <p className="text-sm">{i.summary}</p>
              {i.details.change_pct !== undefined && i.details.change_pct !== null && <p className={`mt-1 text-2xl font-semibold ${i.details.change_pct < 0 ? "text-danger" : "text-success"}`}>{fmtPct(i.details.change_pct)}</p>}
              {i.details.top_segments && i.details.top_segments.length > 0 && <div className="mt-3 flex flex-wrap gap-1.5">{i.details.top_segments.slice(0, 4).map((s, k) => <Badge key={k} tone="accent">{s.dimension}: {s.segment} {fmtPct(s.change_pct)}</Badge>)}</div>}
              {i.details.factors && i.details.factors.length > 0 && (
                <div className="mt-3"><p className="mb-1 text-[11px] font-medium text-muted">Potential contributing factors</p><ol className="space-y-1">{i.details.factors.map((f, k) => <li key={k} className="flex gap-2 text-xs"><span className="text-muted">{k + 1}.</span><span className="flex-1">{f.statement}</span><Badge tone={f.strength === "strong" ? "success" : f.strength === "medium" ? "warning" : "neutral"}>{f.strength}</Badge></li>)}</ol></div>
              )}
              {i.details.related && i.details.related.length > 0 && <p className="mt-3 text-[11px] text-muted">{i.details.related.length} related metrics checked across {i.details.sources?.join(", ")}.</p>}
              {i.kind === "anomaly" && <Link href={`/assistant?q=${encodeURIComponent(`Why did ${i.title.replace(/ (declined|increased).*$/, "").toLowerCase()} change in ${i.title.match(/in (.*)$/)?.[1] || "the latest period"}?`)}`} className="mt-3 inline-flex"><Button size="sm" icon={<ArrowRight className="h-3.5 w-3.5" />}>Investigate with AI</Button></Link>}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
