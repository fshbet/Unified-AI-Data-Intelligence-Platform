"use client";

import { useAuth, useTheme } from "@/lib/auth";
import { cn } from "./ui";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Activity, BookOpen, Bot, Database, FileSearch, Gauge, GitBranch, History, Layers, Lightbulb, LogOut, Moon, Network, Settings, ShieldCheck, Sparkles, Sun, Table2 } from "lucide-react";
import { useEffect } from "react";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: Gauge },
  { href: "/sources", label: "Data Sources", icon: Database },
  { href: "/datasets", label: "Datasets", icon: Table2 },
  { href: "/semantic", label: "Semantic Layer", icon: Layers },
  { href: "/glossary", label: "Business Glossary", icon: BookOpen },
  { href: "/metrics", label: "Metrics", icon: Activity },
  { href: "/relationships", label: "Relationships", icon: Network },
  { href: "/quality", label: "Data Quality", icon: ShieldCheck },
  { href: "/assistant", label: "AI Assistant", icon: Bot },
  { href: "/insights", label: "Insights", icon: Lightbulb },
  { href: "/history", label: "Query History", icon: History },
  { href: "/audit", label: "Audit Logs", icon: FileSearch, admin: true },
  { href: "/admin", label: "Administration", icon: Settings, admin: true },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { user, loading, logout } = useAuth();
  const { dark, toggle } = useTheme();
  const path = usePathname();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  if (loading || !user) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-muted">
        <Sparkles className="mr-2 h-4 w-4 animate-pulse" /> Loading workspace…
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <aside className="flex w-60 shrink-0 flex-col border-r bg-surface">
        <div className="flex h-14 items-center gap-2 border-b px-4">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-accent text-white">
            <GitBranch className="h-4 w-4" />
          </div>
          <div className="leading-tight">
            <p className="text-sm font-semibold">Data Intelligence</p>
            <p className="text-[10px] text-muted">Enterprise Context Platform</p>
          </div>
        </div>
        <nav className="flex-1 overflow-y-auto p-2">
          {NAV.filter((n) => !n.admin || user.role === "admin").map((n) => {
            const active = path === n.href || path.startsWith(n.href + "/");
            return (
              <Link key={n.href} href={n.href} className={cn("mb-0.5 flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors", active ? "bg-primary/10 text-primary" : "text-muted hover:bg-surface-2 hover:text-text")}>
                <n.icon className="h-4 w-4" />
                {n.label}
              </Link>
            );
          })}
        </nav>
        <div className="border-t p-3">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent">{user.name.slice(0, 1).toUpperCase()}</div>
            <div className="min-w-0 flex-1 leading-tight">
              <p className="truncate text-xs font-medium">{user.name}</p>
              <p className="truncate text-[10px] text-muted capitalize">{user.role}</p>
            </div>
            <button onClick={toggle} title="Toggle theme" className="rounded-md p-1.5 text-muted hover:bg-surface-2">
              {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </button>
            <button onClick={logout} title="Sign out" className="rounded-md p-1.5 text-muted hover:bg-surface-2">
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[1400px] p-6">{children}</div>
      </main>
    </div>
  );
}
