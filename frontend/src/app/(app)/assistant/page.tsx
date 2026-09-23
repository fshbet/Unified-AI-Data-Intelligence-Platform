"use client";

import { api, streamAsk, type SSEEvent } from "@/lib/api";
import { AnswerDetails, Markdown, QueryModal, type Payload } from "@/components/answer";
import { Badge, Button, Spinner, cn } from "@/components/ui";
import { Bookmark, Bot, Loader2, MessageSquarePlus, Search, Send, Share2, Sparkles, Trash2, User, Wrench } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

type Msg = { id: string; role: "user" | "assistant"; content: string; payload?: Payload | null; created_at?: string };
type Conv = { id: string; title: string; updated_at: string; is_saved: boolean };
type Progress = { status?: string; plan?: Payload["plan"]; tools: { name: string; args: Record<string, unknown>; preview?: string }[]; investigation?: { evidence: unknown[]; queries: unknown[] } };

function AssistantInner() {
  const params = useSearchParams();
  const router = useRouter();
  const [convs, setConvs] = useState<Conv[]>([]);
  const [convId, setConvId] = useState<string | null>(params.get("c"));
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [draft, setDraft] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [ref, setRef] = useState<string | null>(null);
  const [refQueries, setRefQueries] = useState<Payload["queries"] | undefined>();
  const bottom = useRef<HTMLDivElement>(null);
  const abort = useRef<AbortController | null>(null);

  const loadConvs = useCallback(() => api<Conv[]>("/assistant/conversations").then(setConvs), []);
  useEffect(() => {
    loadConvs();
    api<string[]>("/assistant/suggestions").then(setSuggestions);
  }, [loadConvs]);
  useEffect(() => {
    if (convId) api<{ messages: Msg[] }>(`/assistant/conversations/${convId}`).then((c) => setMsgs(c.messages)).catch(() => setConvId(null));
    else setMsgs([]);
  }, [convId]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, draft, progress]);

  const ask = useCallback(async (question: string) => {
    if (!question.trim() || busy) return;
    setBusy(true);
    setQ("");
    setDraft("");
    setProgress({ tools: [] });
    setMsgs((m) => [...m, { id: `u-${Date.now()}`, role: "user", content: question }]);
    abort.current = new AbortController();
    let cid = convId;
    try {
      await streamAsk(question, convId, (e: SSEEvent) => {
        switch (e.type) {
          case "conversation":
            cid = e.conversation_id as string;
            break;
          case "status":
            setProgress((p) => ({ ...(p || { tools: [] }), status: e.message as string }));
            break;
          case "plan":
            setProgress((p) => ({ ...(p || { tools: [] }), plan: e.plan as Payload["plan"] }));
            break;
          case "investigation":
            setProgress((p) => ({ ...(p || { tools: [] }), investigation: e.investigation as Progress["investigation"] }));
            break;
          case "tool":
            setProgress((p) => ({ ...(p || { tools: [] }), tools: [...(p?.tools || []), { name: e.name as string, args: e.args as Record<string, unknown> }] }));
            break;
          case "tool_result":
            setProgress((p) => { const tools = [...(p?.tools || [])]; const last = tools.findLast((t) => t.name === e.name && !t.preview); if (last) last.preview = e.preview as string; return { ...(p || { tools: [] }), tools }; });
            break;
          case "token":
            setDraft((d) => d + (e.text as string));
            break;
          case "done":
            setMsgs((m) => [...m, { id: e.message_id as string, role: "assistant", content: e.content as string, payload: e.payload as Payload }]);
            setDraft("");
            break;
          case "error":
            setMsgs((m) => [...m, { id: `e-${Date.now()}`, role: "assistant", content: `⚠ ${e.message}` }]);
            break;
        }
      }, abort.current.signal);
    } catch (err) {
      setMsgs((m) => [...m, { id: `e-${Date.now()}`, role: "assistant", content: `⚠ ${(err as Error).message}` }]);
    } finally {
      setBusy(false);
      setProgress(null);
      if (cid && cid !== convId) {
        setConvId(cid);
        router.replace(`/assistant?c=${cid}`);
      }
      loadConvs();
    }
  }, [busy, convId, loadConvs, router]);

  useEffect(() => {
    const pre = params.get("q");
    if (pre && !busy && msgs.length === 0 && !convId) {
      router.replace("/assistant");
      ask(pre);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  const openRef = (r: string, queries?: Payload["queries"]) => {
    setRefQueries(queries);
    setRef(r);
  };
  const lastPayload = [...msgs].reverse().find((m) => m.payload)?.payload;

  return (
    <div className="-m-6 flex h-[calc(100vh)] overflow-hidden">
      <aside className="flex w-64 shrink-0 flex-col border-r bg-surface">
        <div className="border-b p-3"><Button className="w-full" variant="outline" icon={<MessageSquarePlus className="h-4 w-4" />} onClick={() => { setConvId(null); router.replace("/assistant"); }}>New analysis</Button></div>
        <div className="flex-1 overflow-y-auto p-2">
          {convs.map((c) => (
            <button key={c.id} onClick={() => { setConvId(c.id); router.replace(`/assistant?c=${c.id}`); }} className={cn("group mb-0.5 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs", convId === c.id ? "bg-primary/10 text-primary" : "text-muted hover:bg-surface-2 hover:text-text")}>
              {c.is_saved ? <Bookmark className="h-3.5 w-3.5 shrink-0 fill-current" /> : <Bot className="h-3.5 w-3.5 shrink-0" />}
              <span className="flex-1 truncate">{c.title}</span>
              <Trash2 className="hidden h-3.5 w-3.5 shrink-0 text-muted hover:text-danger group-hover:block" onClick={(e) => { e.stopPropagation(); api(`/assistant/conversations/${c.id}`, { method: "DELETE" }).then(() => { if (convId === c.id) setConvId(null); loadConvs(); }); }} />
            </button>
          ))}
          {convs.length === 0 && <p className="p-3 text-xs text-muted">Your analyses will appear here.</p>}
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b bg-surface px-5">
          <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-accent" /><h1 className="text-sm font-semibold">Enterprise Data Assistant</h1><span className="text-xs text-muted">· investigates across every connected domain, cites every number</span></div>
          {convId && <div className="flex gap-1"><Button size="sm" variant="ghost" icon={<Bookmark className="h-3.5 w-3.5" />} onClick={() => api(`/assistant/conversations/${convId}`, { method: "PATCH", json: { is_saved: !convs.find((c) => c.id === convId)?.is_saved } }).then(loadConvs)}>{convs.find((c) => c.id === convId)?.is_saved ? "Saved" : "Save"}</Button><Button size="sm" variant="ghost" icon={<Share2 className="h-3.5 w-3.5" />} onClick={() => navigator.clipboard.writeText(location.href)}>Share link</Button></div>}
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-6">
          <div className="mx-auto max-w-4xl space-y-6">
            {msgs.length === 0 && !busy && (
              <div className="fade-in py-10 text-center">
                <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-primary to-accent text-white shadow-lg"><Bot className="h-7 w-7" /></div>
                <h2 className="text-lg font-semibold">Ask anything about the organisation</h2>
                <p className="mx-auto mt-1 max-w-lg text-sm text-muted">Questions are planned, investigated across finance, sales, HR, inventory, support and more, then answered with traceable evidence, SQL and charts.</p>
                <div className="mx-auto mt-6 grid max-w-2xl gap-2 sm:grid-cols-2">
                  {suggestions.map((s) => <button key={s} onClick={() => ask(s)} className="rounded-xl border bg-surface px-4 py-3 text-left text-sm transition-colors hover:border-primary/50 hover:bg-primary/5"><Search className="mr-2 inline h-3.5 w-3.5 text-muted" />{s}</button>)}
                </div>
              </div>
            )}
            {msgs.map((m) => (
              <div key={m.id} className={cn("fade-in flex gap-3", m.role === "user" && "justify-end")}>
                {m.role === "assistant" && <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent text-white"><Bot className="h-4 w-4" /></div>}
                <div className={cn("min-w-0 rounded-2xl px-4 py-3", m.role === "user" ? "max-w-[75%] bg-primary text-primary-fg" : "w-full border bg-surface")}>
                  {m.role === "user" ? <p className="text-sm">{m.content}</p> : (
                    <>
                      <Markdown text={m.content} onRef={(r) => openRef(r, m.payload?.queries)} />
                      {m.payload && <AnswerDetails payload={m.payload} content={m.content} onRef={(r) => openRef(r, m.payload?.queries)} />}
                      {m.payload && m.payload.follow_ups.length > 0 && m === msgs[msgs.length - 1] && <div className="mt-3 flex flex-wrap gap-1.5">{m.payload.follow_ups.map((f) => <button key={f} onClick={() => ask(f)} className="rounded-full border px-3 py-1 text-xs text-muted transition-colors hover:border-primary hover:text-primary">{f}</button>)}</div>}
                    </>
                  )}
                </div>
                {m.role === "user" && <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted"><User className="h-4 w-4" /></div>}
              </div>
            ))}
            {busy && (
              <div className="fade-in flex gap-3">
                <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent text-white"><Bot className="h-4 w-4" /></div>
                <div className="w-full rounded-2xl border bg-surface px-4 py-3">
                  {progress && (
                    <div className="mb-3 space-y-2 rounded-lg bg-surface-2/60 p-3 text-xs">
                      <p className="flex items-center gap-2 font-medium"><Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />{progress.status || "Starting…"}</p>
                      {progress.plan && <p className="text-muted">Plan: <Badge tone="accent">{progress.plan.intent}</Badge> {progress.plan.metric && <>metric <b>{progress.plan.metric}</b></>} {progress.plan.period && <>· {progress.plan.period.label} vs {progress.plan.comparison?.label}</>} {progress.plan.is_follow_up && <Badge tone="info">follow-up</Badge>}</p>}
                      {progress.plan && progress.plan.steps.length > 0 && <ol className="list-decimal pl-5 text-muted">{progress.plan.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>}
                      {progress.investigation && <p className="text-success">✓ Investigation complete: {progress.investigation.evidence.length} evidence items from {progress.investigation.queries.length} queries</p>}
                      {progress.tools.map((t, i) => <p key={i} className="flex items-start gap-1.5 font-mono text-[11px] text-muted"><Wrench className="mt-0.5 h-3 w-3 shrink-0" /><span><b>{t.name}</b>({Object.entries(t.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ").slice(0, 120)}) {t.preview ? <span className="text-success">✓</span> : <span className="pulse-dot">…</span>}</span></p>)}
                    </div>
                  )}
                  {draft ? <Markdown text={draft} onRef={(r) => openRef(r, lastPayload?.queries)} /> : <p className="flex gap-1 text-muted"><span className="pulse-dot">●</span><span className="pulse-dot [animation-delay:200ms]">●</span><span className="pulse-dot [animation-delay:400ms]">●</span></p>}
                </div>
              </div>
            )}
            <div ref={bottom} />
          </div>
        </div>
        <div className="border-t bg-surface p-4">
          <form onSubmit={(e) => { e.preventDefault(); ask(q); }} className="mx-auto flex max-w-4xl items-end gap-2">
            <textarea value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(q); } }} rows={1} placeholder="Why did revenue drop last month? Which region drove it? Is attrition affecting sales?" className="max-h-40 min-h-[44px] flex-1 resize-y rounded-xl border bg-bg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--ring)]" />
            {busy ? <Button type="button" variant="outline" onClick={() => abort.current?.abort()}>Stop</Button> : <Button type="submit" size="lg" icon={<Send className="h-4 w-4" />} disabled={!q.trim()}>Analyze</Button>}
          </form>
          <p className="mx-auto mt-1.5 max-w-4xl text-[11px] text-muted">Read-only queries · permission-aware · every figure cites a query reference · correlation is never presented as causation.</p>
        </div>
      </div>
      <QueryModal refId={ref} onClose={() => setRef(null)} queries={refQueries} />
    </div>
  );
}

export default function AssistantPage() {
  return <Suspense fallback={<Spinner />}><AssistantInner /></Suspense>;
}
