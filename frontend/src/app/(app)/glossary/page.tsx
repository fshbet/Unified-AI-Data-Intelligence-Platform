"use client";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Card, Empty, Field, Input, Modal, PageHeader, Spinner, Textarea } from "@/components/ui";
import { BookOpen, Pencil, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

type Term = { id: string; term: string; definition: string; domain?: string | null; owner?: string | null; synonyms: string[]; related_terms: string[]; rules: string[] };

export default function GlossaryPage() {
  const { user } = useAuth();
  const [terms, setTerms] = useState<Term[] | null>(null);
  const [edit, setEdit] = useState<Term | null | "new">(null);
  const [q, setQ] = useState("");
  const load = useCallback(() => api<Term[]>("/semantic/glossary").then(setTerms), []);
  useEffect(() => {
    load();
  }, [load]);
  const canEdit = user?.role !== "viewer";
  const list = (terms || []).filter((t) => !q || `${t.term} ${t.definition} ${t.synonyms.join(" ")}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <div>
      <PageHeader title="Business Glossary" description="Official business definitions, synonyms and rules. The AI prefers these over raw column names." actions={<><Input className="w-56" placeholder="Filter terms…" value={q} onChange={(e) => setQ(e.target.value)} />{canEdit && <Button icon={<Plus className="h-4 w-4" />} onClick={() => setEdit("new")}>Add term</Button>}</>} />
      {!terms ? <Spinner /> : list.length === 0 ? <Empty icon={<BookOpen className="h-6 w-6" />} title="No glossary terms" description="Capture how your organisation defines revenue, attrition, active customer…" /> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {list.map((t) => (
            <Card key={t.id} title={t.term} subtitle={<span>{t.domain && <Badge tone="accent">{t.domain}</Badge>} {t.owner && <span className="ml-1">Owner: {t.owner}</span>}</span>} actions={canEdit && <><button className="text-muted hover:text-primary" onClick={() => setEdit(t)}><Pencil className="h-3.5 w-3.5" /></button>{user?.role === "admin" && <button className="text-muted hover:text-danger" onClick={() => confirm("Delete term?") && api(`/semantic/glossary/${t.id}`, { method: "DELETE" }).then(load)}><Trash2 className="h-3.5 w-3.5" /></button>}</>}>
              <p className="text-sm text-text/85">{t.definition}</p>
              {t.rules.length > 0 && <div className="mt-3"><p className="mb-1 text-[11px] font-medium text-muted">Business rules</p><ul className="space-y-1">{t.rules.map((r, i) => <li key={i} className="rounded bg-surface-2 px-2 py-1 font-mono text-[11px]">{r}</li>)}</ul></div>}
              {(t.synonyms.length > 0 || t.related_terms.length > 0) && <div className="mt-3 flex flex-wrap gap-1 text-[11px]">{t.synonyms.map((s) => <Badge key={s}>{s}</Badge>)}{t.related_terms.map((s) => <Badge key={s} tone="info">↔ {s}</Badge>)}</div>}
            </Card>
          ))}
        </div>
      )}
      <Modal open={!!edit} onClose={() => setEdit(null)} title={edit === "new" ? "New glossary term" : `Edit ${(edit as Term)?.term}`}>
        {edit && <TermForm initial={edit === "new" ? null : edit} onDone={() => { setEdit(null); load(); }} />}
      </Modal>
    </div>
  );
}

function TermForm({ initial, onDone }: { initial: Term | null; onDone: () => void }) {
  const [f, setF] = useState({ term: initial?.term || "", definition: initial?.definition || "", domain: initial?.domain || "", owner: initial?.owner || "", synonyms: (initial?.synonyms || []).join(", "), related_terms: (initial?.related_terms || []).join(", "), rules: (initial?.rules || []).join("\n") });
  const split = (s: string, sep: RegExp) => s.split(sep).map((x) => x.trim()).filter(Boolean);
  const save = () => {
    const body = { term: f.term, definition: f.definition, domain: f.domain || null, owner: f.owner || null, synonyms: split(f.synonyms, /,/), related_terms: split(f.related_terms, /,/), rules: split(f.rules, /\n/) };
    return (initial ? api(`/semantic/glossary/${initial.id}`, { method: "PUT", json: body }) : api("/semantic/glossary", { method: "POST", json: body })).then(onDone);
  };
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-3">
        <Field label="Term"><Input value={f.term} onChange={(e) => setF({ ...f, term: e.target.value })} /></Field>
        <Field label="Domain"><Input value={f.domain} onChange={(e) => setF({ ...f, domain: e.target.value })} /></Field>
        <Field label="Owner"><Input value={f.owner} onChange={(e) => setF({ ...f, owner: e.target.value })} /></Field>
      </div>
      <Field label="Definition"><Textarea value={f.definition} onChange={(e) => setF({ ...f, definition: e.target.value })} /></Field>
      <Field label="Synonyms (comma separated)"><Input value={f.synonyms} onChange={(e) => setF({ ...f, synonyms: e.target.value })} /></Field>
      <Field label="Related terms (comma separated)"><Input value={f.related_terms} onChange={(e) => setF({ ...f, related_terms: e.target.value })} /></Field>
      <Field label="Business rules (one per line)" hint="e.g. Revenue = SUM(invoice_amount) WHERE invoice_status = 'POSTED'"><Textarea value={f.rules} onChange={(e) => setF({ ...f, rules: e.target.value })} className="font-mono text-xs" /></Field>
      <div className="flex justify-end"><Button disabled={!f.term || !f.definition} onClick={save}>Save</Button></div>
    </div>
  );
}
