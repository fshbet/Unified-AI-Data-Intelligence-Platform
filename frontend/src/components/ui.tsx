"use client";

import clsx from "clsx";
import { Loader2, X } from "lucide-react";
import { useEffect, type ReactNode } from "react";

export function cn(...a: (string | false | null | undefined)[]) {
  return clsx(a);
}

export function Card({ className, children, title, subtitle, actions, padded = true }: { className?: string; children: ReactNode; title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; padded?: boolean }) {
  return (
    <div className={cn("rounded-xl border bg-surface shadow-[0_1px_2px_rgba(16,24,40,0.04)]", className)}>
      {(title || actions) && (
        <div className="flex items-start justify-between gap-3 border-b px-5 py-3.5">
          <div>
            {title && <h3 className="text-sm font-semibold">{title}</h3>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={padded ? "p-5" : ""}>{children}</div>
    </div>
  );
}

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "ghost" | "danger" | "outline"; size?: "sm" | "md" | "lg"; loading?: boolean; icon?: ReactNode };
export function Button({ variant = "primary", size = "md", loading, icon, className, children, disabled, ...rest }: BtnProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" && "h-8 px-2.5 text-xs",
        size === "md" && "h-9 px-3.5 text-sm",
        size === "lg" && "h-11 px-5 text-sm",
        variant === "primary" && "bg-primary text-primary-fg hover:brightness-110",
        variant === "secondary" && "bg-surface-2 text-text hover:brightness-95 dark:hover:brightness-125",
        variant === "outline" && "border bg-surface text-text hover:bg-surface-2",
        variant === "ghost" && "text-text hover:bg-surface-2",
        variant === "danger" && "bg-danger text-white hover:brightness-110",
        className,
      )}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}

export function Badge({ children, tone = "neutral", className }: { children: ReactNode; tone?: "neutral" | "success" | "warning" | "danger" | "info" | "accent"; className?: string }) {
  const tones = {
    neutral: "bg-surface-2 text-muted",
    success: "bg-success/12 text-success",
    warning: "bg-warning/15 text-warning",
    danger: "bg-danger/12 text-danger",
    info: "bg-primary/12 text-primary",
    accent: "bg-accent/12 text-accent",
  };
  return <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium whitespace-nowrap", tones[tone], className)}>{children}</span>;
}

export function statusTone(s: string): "neutral" | "success" | "warning" | "danger" | "info" | "accent" {
  const m: Record<string, "neutral" | "success" | "warning" | "danger" | "info" | "accent"> = { connected: "success", imported: "success", ok: "success", approved: "success", done: "success", resolved: "success", error: "danger", critical: "danger", blocked: "danger", rejected: "danger", warning: "warning", suggested: "warning", running: "info", queued: "info", info: "info", pending: "neutral", disabled: "neutral", open: "warning", high: "success", medium: "warning", low: "danger", pii: "danger", restricted: "danger", sensitive: "warning", public: "neutral", new: "info" };
  return m[s] || "neutral";
}

export const inputCls = "h-9 w-full rounded-lg border bg-surface px-3 text-sm text-text placeholder:text-muted/70 focus:outline-none focus:ring-2 focus:ring-[var(--ring)] disabled:opacity-60";

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(inputCls, props.className)} />;
}
export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn(inputCls, "h-auto min-h-[80px] py-2", props.className)} />;
}
export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cn(inputCls, "pr-8", props.className)} />;
}
export function Field({ label, children, hint, className }: { label: string; children: ReactNode; hint?: string; className?: string }) {
  return (
    <label className={cn("block", className)}>
      <span className="mb-1 block text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-muted/80">{hint}</span>}
    </label>
  );
}

export function Modal({ open, onClose, title, children, width = "max-w-xl", footer }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; width?: string; footer?: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]" onClick={onClose}>
      <div className={cn("fade-in max-h-[90vh] w-full overflow-hidden rounded-xl border bg-surface shadow-2xl flex flex-col", width)} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-5 py-3">
          <h3 className="text-sm font-semibold">{title}</h3>
          <button onClick={onClose} className="rounded-md p-1 text-muted hover:bg-surface-2">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="overflow-y-auto p-5">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}

export function Table<T>({ columns, rows, keyFn, empty, onRowClick, dense }: { columns: { key: string; label: ReactNode; render?: (r: T) => ReactNode; className?: string; align?: "left" | "right" }[]; rows: T[]; keyFn: (r: T, i: number) => string; empty?: ReactNode; onRowClick?: (r: T) => void; dense?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-[11px] uppercase tracking-wide text-muted">
            {columns.map((c) => (
              <th key={c.key} className={cn("px-3 py-2 font-medium", c.align === "right" && "text-right", c.className)}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-10 text-center text-sm text-muted">
                {empty || "Nothing here yet."}
              </td>
            </tr>
          )}
          {rows.map((r, i) => (
            <tr key={keyFn(r, i)} onClick={onRowClick ? () => onRowClick(r) : undefined} className={cn("border-b last:border-b-0", onRowClick && "cursor-pointer hover:bg-surface-2/70")}>
              {columns.map((c) => (
                <td key={c.key} className={cn("px-3 align-top", dense ? "py-1.5" : "py-2.5", c.align === "right" && "text-right tabular-nums", c.className)}>
                  {c.render ? c.render(r) : String((r as Record<string, unknown>)[c.key] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Empty({ icon, title, description, action }: { icon?: ReactNode; title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed px-6 py-14 text-center">
      {icon && <div className="mb-3 rounded-full bg-surface-2 p-3 text-muted">{icon}</div>}
      <h3 className="text-sm font-semibold">{title}</h3>
      {description && <p className="mt-1 max-w-sm text-xs text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted">
      <Loader2 className="h-4 w-4 animate-spin" /> {label || "Loading…"}
    </div>
  );
}

export function Stat({ label, value, sub, tone, icon }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "success" | "danger" | "warning" | "neutral"; icon?: ReactNode }) {
  return (
    <div className="rounded-xl border bg-surface p-4">
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium text-muted">{label}</p>
        {icon && <span className="text-muted">{icon}</span>}
      </div>
      <p className={cn("mt-1.5 text-2xl font-semibold tabular-nums tracking-tight", tone === "success" && "text-success", tone === "danger" && "text-danger", tone === "warning" && "text-warning")}>{value}</p>
      {sub && <p className="mt-1 text-xs text-muted">{sub}</p>}
    </div>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: { id: string; label: ReactNode }[]; value: string; onChange: (id: string) => void }) {
  return (
    <div className="flex gap-1 border-b">
      {tabs.map((t) => (
        <button key={t.id} onClick={() => onChange(t.id)} className={cn("-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors", value === t.id ? "border-primary text-primary" : "border-transparent text-muted hover:text-text")}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded border bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-muted">{children}</kbd>;
}

export function Code({ children, className }: { children: string; className?: string }) {
  return <pre className={cn("overflow-x-auto rounded-lg border bg-surface-2 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap", className)}>{children}</pre>;
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 text-sm">
      <span onClick={() => onChange(!checked)} className={cn("relative h-5 w-9 rounded-full transition-colors", checked ? "bg-primary" : "bg-muted/40")}>
        <span className={cn("absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all", checked ? "left-[18px]" : "left-0.5")} />
      </span>
      {label}
    </label>
  );
}
