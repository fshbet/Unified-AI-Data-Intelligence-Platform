"use client";

import { api, fmtNumber, fmtPct } from "@/lib/api";
import { Badge, Button, Code, Modal, Tabs, statusTone } from "./ui";
import { Chart, type ChartSpec } from "./chart";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useMemo, useState, type ReactNode } from "react";
import { Copy, Download } from "lucide-react";

export type Evidence = { id: string; statement: string; kind: string; domain?: string | null; source: string; table: string; query_refs: string[]; strength: string };
export type QueryRec = { ref: string; sql: string; source: string; tables?: string[]; columns?: string[]; rows?: unknown[][]; row_count?: number; error?: string | null; duration_ms?: number };
export type Payload = {
  plan?: { intent: string; metric?: string | null; period?: { label: string } | null; comparison?: { label: string } | null; steps: string[]; domains: string[]; candidate_tables: string[]; is_follow_up: boolean; inherited: string[]; focus?: string | null };
  investigation?: { headline: Record<string, unknown>; reasoning_path: string[]; freshness: { source: string; label: string }[]; quality_issues: { table: string; severity: string; message: string }[]; related: { metric: string; domain?: string | null; change_pct: number | null; before: number | null; after: number | null; path: string; segment?: { dimension: string; segment: string; change_pct: number | null } | null; unit?: string | null; format?: string | null }[]; decompositions: { dimension: string; segments: { segment: string; before: number; after: number; change_pct: number | null; contribution_pct: number | null }[] }[] } | null;
  queries: QueryRec[];
  evidence: Evidence[];
  sources: string[];
  tables: string[];
  charts: ChartSpec[];
  reasoning_path: string[];
  confidence: { level: string; reasons: string[] };
  usage: { provider?: string | null; model?: string | null; calls: number; input_tokens: number; output_tokens: number; estimated_cost: number };
  tool_calls: { tool: string; args: Record<string, unknown>; ok: boolean }[];
  warnings: string[];
  follow_ups: string[];
};

const QREF = /\[?(Q-[0-9A-F]{6,8}(?:⚠unverified)?(?:\s*,\s*Q-[0-9A-F]{6,8}(?:⚠unverified)?)*)\]?/g;

export function QueryModal({ refId, onClose, queries }: { refId: string | null; onClose: () => void; queries?: QueryRec[] }) {
  const [remote, setRemote] = useState<QueryRec | null>(null);
  const local = queries?.find((q) => q.ref === refId);
  useMemo(() => {
    if (refId && !local) api<{ ref: string; sql: string; source: string; result_preview: unknown[][] | null; error?: string | null; duration_ms?: number; row_count?: number }>(`/admin/queries/${refId}`).then((r) => setRemote({ ref: r.ref, sql: r.sql, source: r.source, columns: (r.result_preview?.[0] as string[]) || [], rows: (r.result_preview?.slice(1) as unknown[][]) || [], error: r.error, duration_ms: r.duration_ms, row_count: r.row_count })).catch(() => setRemote(null));
  }, [refId, local]);
  const q = local || remote;
  return (
    <Modal open={!!refId} onClose={onClose} title={<span className="font-mono">{refId}</span>} width="max-w-3xl">
      {!q ? <p className="text-sm text-muted">Loading…</p> : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 text-xs text-muted"><Badge tone="info">{q.source}</Badge>{q.tables?.map((t) => <Badge key={t}>{t}</Badge>)}<span>{q.row_count ?? q.rows?.length ?? 0} rows · {q.duration_ms ?? "?"} ms</span></div>
          <Code>{q.sql}</Code>
          {q.error && <p className="rounded-lg bg-danger/10 px-3 py-2 text-xs text-danger">{q.error}</p>}
          {q.columns && q.columns.length > 0 && <ResultTable columns={q.columns} rows={q.rows || []} />}
        </div>
      )}
    </Modal>
  );
}

export function ResultTable({ columns, rows, max = 50 }: { columns: string[]; rows: unknown[][]; max?: number }) {
  return (
    <div className="max-h-80 overflow-auto rounded-lg border">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-surface-2"><tr>{columns.map((c) => <th key={c} className="px-2 py-1.5 text-left font-mono font-medium">{c}</th>)}</tr></thead>
        <tbody>{rows.slice(0, max).map((r, i) => <tr key={i} className="border-t">{r.map((v, j) => <td key={j} className="max-w-[260px] truncate px-2 py-1 font-mono tabular-nums">{v === null || v === undefined ? <span className="text-muted">null</span> : typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function withRefs(text: string, onRef: (r: string) => void): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  const re = new RegExp(QREF.source, "g");
  while ((m = re.exec(text))) {
    out.push(text.slice(last, m.index));
    m[1].split(/\s*,\s*/).forEach((r, i) =>
      out.push(
        r.endsWith("unverified") ? (
          <span key={`${m!.index}-${i}`} className="qref mx-0.5 opacity-60 line-through" title="This citation does not match any executed query and could not be verified">{r.replace("⚠unverified", "")} ⚠</span>
        ) : (
          <span key={`${m!.index}-${i}`} className="qref mx-0.5" onClick={() => onRef(r)}>{r}</span>
        ),
      ),
    );
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

export function Markdown({ text, onRef }: { text: string; onRef: (r: string) => void }) {
  return (
    <div className="prose-answer text-sm">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ p: ({ children }) => <p>{mapRefs(children, onRef)}</p>, li: ({ children }) => <li>{mapRefs(children, onRef)}</li>, td: ({ children }) => <td>{mapRefs(children, onRef)}</td>, strong: ({ children }) => <strong>{mapRefs(children, onRef)}</strong>, h2: ({ children }) => <h2>{children}</h2> }}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function mapRefs(children: ReactNode, onRef: (r: string) => void): ReactNode {
  if (typeof children === "string") return withRefs(children, onRef);
  if (Array.isArray(children)) return children.map((c, i) => (typeof c === "string" ? <span key={i}>{withRefs(c, onRef)}</span> : c));
  return children;
}

export function AnswerDetails({ payload, onRef, content }: { payload: Payload; onRef: (r: string) => void; content: string }) {
  const [tab, setTab] = useState("evidence");
  const inv = payload.investigation;
  const tabs = [
    { id: "evidence", label: `Evidence (${payload.evidence.length})` },
    { id: "charts", label: `Charts (${payload.charts.length})` },
    { id: "sql", label: `SQL (${payload.queries.length})` },
    { id: "sources", label: "Sources & tables" },
    { id: "plan", label: "Plan & reasoning" },
  ];
  const exportMd = () => {
    const blob = new Blob([`${content}\n\n---\nSources: ${payload.sources.join(", ")}\nTables: ${payload.tables.join(", ")}\nConfidence: ${payload.confidence.level}\n\nQueries:\n${payload.queries.map((q) => `${q.ref} [${q.source}]\n${q.sql}\n`).join("\n")}`], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "analysis.md";
    a.click();
  };
  return (
    <div className="mt-4 rounded-xl border bg-surface-2/40">
      <div className="flex items-center justify-between px-3 pt-1">
        <Tabs tabs={tabs} value={tab} onChange={setTab} />
        <div className="flex items-center gap-2 pb-1">
          <Badge tone={statusTone(payload.confidence.level)}>confidence: {payload.confidence.level}</Badge>
          <Button size="sm" variant="ghost" icon={<Copy className="h-3.5 w-3.5" />} onClick={() => navigator.clipboard.writeText(content)}>Copy</Button>
          <Button size="sm" variant="ghost" icon={<Download className="h-3.5 w-3.5" />} onClick={exportMd}>Export</Button>
        </div>
      </div>
      <div className="p-4">
        {tab === "evidence" && (
          <div className="space-y-2">
            {payload.evidence.length === 0 && <p className="text-xs text-muted">No structured evidence — the answer relied on tool results cited inline.</p>}
            {payload.evidence.map((e) => (
              <div key={e.id} className="flex items-start gap-3 rounded-lg border bg-surface p-3 text-sm">
                <span className="mt-0.5 font-mono text-[11px] text-muted">{e.id}</span>
                <div className="min-w-0 flex-1">
                  <p>{e.statement}</p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px]"><Badge tone={e.kind === "correlation" ? "warning" : e.kind === "data_quality" ? "danger" : "info"}>{e.kind.replace(/_/g, " ")}</Badge><Badge tone={e.strength === "strong" ? "success" : e.strength === "medium" ? "warning" : "neutral"}>{e.strength} evidence</Badge>{e.domain && <Badge tone="accent">{e.domain}</Badge>}<span className="text-muted">{e.source} → <span className="font-mono">{e.table}</span></span>{e.query_refs.map((r) => <span key={r} className="qref" onClick={() => onRef(r)}>{r}</span>)}</div>
                </div>
              </div>
            ))}
            {inv && inv.decompositions.length > 0 && (
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                {inv.decompositions.slice(0, 4).map((d) => (
                  <div key={d.dimension} className="rounded-lg border bg-surface p-3"><p className="mb-1 text-xs font-medium">By {d.dimension}</p><table className="w-full text-xs"><tbody>{d.segments.slice(0, 6).map((s) => <tr key={s.segment} className="border-t"><td className="py-1 font-mono">{s.segment}</td><td className="py-1 text-right tabular-nums text-muted">{fmtNumber(s.before, { format: String(inv.headline.format || ""), unit: String(inv.headline.unit || "") })} → {fmtNumber(s.after, { format: String(inv.headline.format || ""), unit: String(inv.headline.unit || "") })}</td><td className={`py-1 text-right tabular-nums ${(s.change_pct || 0) < 0 ? "text-danger" : "text-success"}`}>{fmtPct(s.change_pct)}</td><td className="py-1 text-right tabular-nums text-muted">{s.contribution_pct !== null ? `${s.contribution_pct.toFixed(0)}% of Δ` : ""}</td></tr>)}</tbody></table></div>
                ))}
              </div>
            )}
            {payload.warnings.length > 0 && <div className="rounded-lg bg-warning/10 p-3 text-xs text-warning">{payload.warnings.map((w, i) => <p key={i}>⚠ {w}</p>)}</div>}
          </div>
        )}
        {tab === "charts" && <div className="grid gap-3 md:grid-cols-2">{payload.charts.map((c, i) => <Chart key={i} spec={c} />)}{payload.charts.length === 0 && <p className="text-xs text-muted">No charts for this answer.</p>}</div>}
        {tab === "sql" && (
          <div className="space-y-2">
            {payload.queries.map((q) => (
              <details key={q.ref} className="rounded-lg border bg-surface">
                <summary className="flex cursor-pointer items-center gap-2 px-3 py-2 text-xs"><span className="qref" onClick={(e) => { e.preventDefault(); onRef(q.ref); }}>{q.ref}</span><Badge tone="info">{q.source}</Badge><span className="truncate font-mono text-muted">{q.sql.slice(0, 110)}</span>{q.error && <Badge tone="danger">error</Badge>}<span className="ml-auto text-muted">{q.row_count ?? q.rows?.length ?? 0} rows · {q.duration_ms ?? "?"}ms</span></summary>
                <div className="border-t p-3"><Code>{q.sql}</Code>{q.error ? <p className="mt-2 text-xs text-danger">{q.error}</p> : q.columns && q.columns.length > 0 && <div className="mt-2"><ResultTable columns={q.columns} rows={q.rows || []} max={15} /></div>}</div>
              </details>
            ))}
          </div>
        )}
        {tab === "sources" && (
          <div className="grid gap-4 md:grid-cols-3 text-sm">
            <div><p className="mb-1 text-xs font-medium text-muted">Data sources used</p>{payload.sources.map((s) => <Badge key={s} tone="info" className="mr-1 mb-1">{s}</Badge>)}</div>
            <div><p className="mb-1 text-xs font-medium text-muted">Tables used</p>{payload.tables.map((t) => <Badge key={t} className="mr-1 mb-1 font-mono">{t}</Badge>)}</div>
            <div><p className="mb-1 text-xs font-medium text-muted">Data freshness</p>{inv?.freshness.map((f) => <p key={f.source} className="text-xs">{f.source}: <span className="text-muted">updated {f.label}</span></p>)}{inv?.quality_issues.length ? <div className="mt-2"><p className="mb-1 text-xs font-medium text-muted">Open quality issues</p>{inv.quality_issues.map((q, i) => <p key={i} className="text-xs text-warning">⚠ {q.message}</p>)}</div> : null}</div>
          </div>
        )}
        {tab === "plan" && payload.plan && (
          <div className="grid gap-4 md:grid-cols-2 text-sm">
            <div>
              <p className="mb-1 text-xs font-medium text-muted">Query plan</p>
              <div className="rounded-lg border bg-surface p-3 text-xs"><p>Intent: <Badge tone="accent">{payload.plan.intent}</Badge> {payload.plan.is_follow_up && <Badge tone="info">follow-up · inherited {payload.plan.inherited.join(", ")}</Badge>}</p>{payload.plan.metric && <p className="mt-1">Metric: <b>{payload.plan.metric}</b>{payload.plan.focus && <> · focus: <b>{payload.plan.focus}</b></>}</p>}{payload.plan.period && <p className="mt-1">Period: {payload.plan.period.label} vs {payload.plan.comparison?.label}</p>}{payload.plan.domains.length > 0 && <p className="mt-1">Domains: {payload.plan.domains.join(", ")}</p>}</div>
              <p className="mb-1 mt-3 text-xs font-medium text-muted">Investigation plan</p>
              <ol className="list-decimal space-y-0.5 pl-5 text-xs">{payload.plan.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
            </div>
            <div>
              <p className="mb-1 text-xs font-medium text-muted">Reasoning path</p>
              <div className="flex flex-wrap items-center gap-1 text-xs">{payload.reasoning_path.map((t, i) => <span key={t} className="flex items-center gap-1"><span className="rounded-md border bg-surface px-2 py-1 font-mono">{t}</span>{i < payload.reasoning_path.length - 1 && <span className="text-muted">→</span>}</span>)}</div>
              {payload.tool_calls.length > 0 && <><p className="mb-1 mt-3 text-xs font-medium text-muted">AI tool calls</p><ul className="space-y-0.5 text-xs">{payload.tool_calls.map((t, i) => <li key={i} className="font-mono"><span className={t.ok ? "text-success" : "text-danger"}>●</span> {t.tool}({Object.entries(t.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ").slice(0, 100)})</li>)}</ul></>}
              <p className="mb-1 mt-3 text-xs font-medium text-muted">Confidence</p>
              <ul className="text-xs">{payload.confidence.reasons.map((r, i) => <li key={i}>• {r}</li>)}</ul>
              <p className="mt-3 text-[11px] text-muted">{payload.usage.model ? `${payload.usage.provider}/${payload.usage.model} · ${payload.usage.calls} calls · ${(payload.usage.input_tokens + payload.usage.output_tokens).toLocaleString()} tokens` : "No LLM used (deterministic)"}</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
