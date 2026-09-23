"use client";

export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("edi_token");
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers, body });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/login")) {
    localStorage.removeItem("edi_token");
    window.location.href = "/login";
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export type SSEEvent = { type: string; [k: string]: unknown };

/** POST + read Server-Sent Events. Calls onEvent for each event; resolves when the stream closes. */
export async function streamAsk(question: string, conversationId: string | null, onEvent: (e: SSEEvent) => void, signal?: AbortSignal): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "text/event-stream" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/assistant/ask`, { method: "POST", headers, body: JSON.stringify({ question, conversation_id: conversationId }), signal });
  if (!res.ok || !res.body) throw new ApiError(res.status, await res.text());
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const dataLines = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim());
      if (!dataLines.length) continue;
      try {
        onEvent(JSON.parse(dataLines.join("\n")) as SSEEvent);
      } catch {
        /* keep-alive or malformed */
      }
    }
  }
}

export function fmtNumber(v: number | null | undefined, opts: { format?: string | null; unit?: string | null; compact?: boolean } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const { format, unit, compact = true } = opts;
  if (format === "percent") return `${v.toFixed(1)}%`;
  const abs = Math.abs(v);
  let s: string;
  if (compact && abs >= 1e7 && (unit === "INR" || format === "currency")) s = `₹${(v / 1e7).toFixed(2)} Cr`;
  else if (compact && abs >= 1e5 && (unit === "INR" || format === "currency")) s = `₹${(v / 1e5).toFixed(2)} L`;
  else if (compact && abs >= 1e6) s = `${(v / 1e6).toFixed(2)}M`;
  else if (compact && abs >= 1e4) s = `${(v / 1e3).toFixed(1)}K`;
  else s = Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (format === "currency" && !s.startsWith("₹")) s = `${unit === "INR" ? "₹" : unit ? unit + " " : ""}${s}`;
  else if (unit && unit !== "INR" && unit !== "%" && !s.startsWith("₹")) s = `${s} ${unit}`;
  return s;
}

export function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
}

/** API timestamps are UTC; naive ISO strings (no offset) must not be parsed as local time. */
export function parseDate(iso: string): Date {
  return new Date(/[Zz]$|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
}

export function timeAgo(iso?: string | null): string {
  if (!iso) return "never";
  const d = parseDate(iso);
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
