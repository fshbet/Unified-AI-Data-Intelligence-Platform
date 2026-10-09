"use client";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Card, Empty, PageHeader, Select, Spinner, Stat, Toggle } from "@/components/ui";
import { AlertTriangle, EyeOff, Lock, Search, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

type Policy = {
  column_id: string; table_id: string; table_name: string; source_name: string | null;
  column_name: string; logical_type: string; pii_type: string | null; sensitivity: string;
  strategy: string; entity: string; params: Record<string, unknown>;
  is_default: boolean; suggested: string; accepted_by: string | null;
  sample: string | null; preview: string | null;
};

type Summary = {
  columns_total: number; columns_protected: number;
  protected_by_default_not_yet_reviewed: number;
  unprotected_detected_pii: { column_id: string; table: string; column: string; pii_type: string | null }[];
  strategies: string[]; entities: string[];
};

const EXPLAIN: Record<string, string> = {
  passthrough: "The real value is sent to the AI.",
  pseudonym: "Replaced with a reversible token. Restored in the answer.",
  redact: "Removed entirely. Cannot be recovered.",
  mask: "Partially hidden. Cannot be recovered.",
  hash: "Stable fingerprint — joins still work, the value cannot be read.",
  generalize: "Replaced with a band, keeping the magnitude.",
  shift: "Dates moved by a constant, so intervals stay accurate.",
};

export default function PrivacyPage() {
  const { user } = useAuth();
  const readOnly = user?.role === "viewer";
  const [rows, setRows] = useState<Policy[] | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [q, setQ] = useState("");
  const [onlyProtected, setOnlyProtected] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [p, s] = await Promise.all([api<Policy[]>("/privacy/policies"), api<Summary>("/privacy/summary")]);
    setRows(p);
    setSummary(s);
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async (row: Policy, patch: Partial<Pick<Policy, "strategy" | "entity">>) => {
    setSaving(row.column_id);
    try {
      const updated = await api<Policy>(`/privacy/policies/${row.column_id}`, {
        method: "PUT",
        json: { strategy: patch.strategy ?? row.strategy, entity: patch.entity ?? row.entity, params: row.params },
      });
      setRows((cur) => (cur || []).map((r) => (r.column_id === updated.column_id ? updated : r)));
      api<Summary>("/privacy/summary").then(setSummary);
    } finally {
      setSaving(null);
    }
  };

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (rows || []).filter((r) => {
      if (onlyProtected && r.strategy === "passthrough") return false;
      if (!needle) return true;
      return `${r.table_name} ${r.column_name} ${r.pii_type ?? ""}`.toLowerCase().includes(needle);
    });
  }, [rows, q, onlyProtected]);

  const grouped = useMemo(() => {
    const m = new Map<string, Policy[]>();
    for (const r of visible) m.set(r.table_name, [...(m.get(r.table_name) || []), r]);
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [visible]);

  return (
    <div>
      <PageHeader
        title="AI Privacy"
        description="What the AI is allowed to see. This is separate from user permissions: a person entitled to view every row can still have the model see only tokens. Protected values are replaced before the request leaves this machine and restored in the answer."
        actions={!readOnly && summary && summary.protected_by_default_not_yet_reviewed > 0 && (
          <Button variant="outline" icon={<ShieldCheck className="h-4 w-4" />}
            onClick={async () => { await api("/privacy/policies/accept-suggested", { method: "POST" }); await load(); }}>
            Confirm {summary.protected_by_default_not_yet_reviewed} detected
          </Button>
        )}
      />

      <div className="mb-6 grid grid-cols-3 gap-4">
        <Stat label="Columns protected" value={summary ? summary.columns_protected : "—"} tone="success" />
        <Stat label="Detected, not yet confirmed" value={summary ? summary.protected_by_default_not_yet_reviewed : "—"}
          tone={summary?.protected_by_default_not_yet_reviewed ? "warning" : undefined} />
        <Stat label="Detected PII left unprotected" value={summary ? summary.unprotected_detected_pii.length : "—"}
          tone={summary?.unprotected_detected_pii.length ? "danger" : "success"} />
      </div>

      {summary && summary.unprotected_detected_pii.length > 0 && (
        <Card className="mb-6 border-danger/40 bg-danger/5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
            <div className="text-sm">
              <p className="font-medium">These columns were detected as personal data but are sent to the AI as-is.</p>
              <p className="mt-1 text-muted">
                {summary.unprotected_detected_pii.slice(0, 6).map((c) => `${c.table}.${c.column}`).join(", ")}
                {summary.unprotected_detected_pii.length > 6 && ` and ${summary.unprotected_detected_pii.length - 6} more`}
              </p>
            </div>
          </div>
        </Card>
      )}

      <Card padded={false} title="Column policies" actions={
        <>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter columns…"
              className="h-8 w-52 rounded-md border bg-transparent pl-8 pr-2 text-xs outline-none focus:border-primary" />
          </div>
          <Toggle checked={onlyProtected} onChange={setOnlyProtected} label="Protected only" />
        </>
      }>
        {!rows ? <Spinner /> : grouped.length === 0 ? (
          <div className="p-6"><Empty icon={<EyeOff className="h-6 w-6" />} title="Nothing matches" description="Adjust the filter above." /></div>
        ) : (
          <div className="divide-y">
            {grouped.map(([table, cols]) => (
              <div key={table}>
                <div className="flex items-center gap-2 bg-surface-2/60 px-5 py-2">
                  <span className="font-mono text-xs font-medium">{table}</span>
                  <span className="text-[11px] text-muted">{cols[0]?.source_name}</span>
                  <span className="ml-auto text-[11px] text-muted">
                    {cols.filter((c) => c.strategy !== "passthrough").length} of {cols.length} protected
                  </span>
                </div>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-[10px] uppercase tracking-wider text-muted">
                      <th className="px-5 py-2 font-medium">Column</th>
                      <th className="px-3 py-2 font-medium">Detected</th>
                      <th className="px-3 py-2 font-medium">Sample</th>
                      <th className="px-3 py-2 font-medium">Treatment</th>
                      <th className="px-5 py-2 font-medium">What the AI receives</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {cols.map((r) => {
                      const off = r.strategy === "passthrough";
                      return (
                        <tr key={r.column_id} className={saving === r.column_id ? "opacity-50" : undefined}>
                          <td className="px-5 py-2">
                            <div className="flex items-center gap-2">
                              {!off && <Lock className="h-3 w-3 shrink-0 text-success" aria-label="protected" />}
                              <span className="font-mono text-xs">{r.column_name}</span>
                            </div>
                            <span className="text-[10px] text-muted">{r.logical_type}</span>
                          </td>
                          <td className="px-3 py-2">
                            {r.pii_type ? <Badge tone="warning">{r.pii_type}</Badge> : <span className="text-[11px] text-muted">—</span>}
                            {r.is_default && !off && <div className="mt-0.5 text-[10px] text-muted">auto · unconfirmed</div>}
                          </td>
                          {/* Masked on purpose: a privacy screen must not display the data it protects. */}
                          <td className="px-3 py-2 font-mono text-[11px] text-muted">{r.sample ?? "—"}</td>
                          <td className="px-3 py-2">
                            <Select className="w-36" value={r.strategy} disabled={readOnly || saving === r.column_id}
                              onChange={(e) => save(r, { strategy: e.target.value })}>
                              {(summary?.strategies || []).map((s) => <option key={s} value={s}>{s}</option>)}
                            </Select>
                            {r.strategy === "pseudonym" && (
                              <Select className="mt-1 w-36" value={r.entity} disabled={readOnly || saving === r.column_id}
                                onChange={(e) => save(r, { entity: e.target.value })}>
                                {(summary?.entities || []).map((s) => <option key={s} value={s}>{s}</option>)}
                              </Select>
                            )}
                          </td>
                          <td className="px-5 py-2">
                            <span className={`font-mono text-xs ${off ? "text-danger" : "text-success"}`}>
                              {r.preview ?? (off ? "the real value" : "—")}
                            </span>
                            <div className="text-[10px] text-muted">{EXPLAIN[r.strategy]}</div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        )}
      </Card>

      <p className="mt-4 text-[11px] leading-relaxed text-muted">
        Values shorter than three characters are not covered by the outbound safety check, and a
        value that appears only in free text and matches none of the detectors may not be caught.
        Numeric columns are passed through by default so the assistant can still calculate.
      </p>
    </div>
  );
}
