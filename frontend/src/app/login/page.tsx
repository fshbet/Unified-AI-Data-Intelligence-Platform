"use client";

import { useAuth } from "@/lib/auth";
import { Button, Field, Input } from "@/components/ui";
import { GitBranch } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

export default function LoginPage() {
  const { login, user, loading } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("admin123");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!loading && user) router.replace("/dashboard");
  }, [user, loading, router]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await login(email, password);
      router.replace("/dashboard");
    } catch (ex) {
      setErr((ex as Error).message || "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-[radial-gradient(ellipse_at_top,_color-mix(in_oklab,var(--primary)_18%,transparent),transparent_60%)] p-6">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-accent text-white shadow-lg">
            <GitBranch className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-lg font-semibold">Enterprise Data Intelligence</h1>
            <p className="text-xs text-muted">Unified semantic layer · AI reasoning · Evidence-based answers</p>
          </div>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-xl border bg-surface p-6 shadow-xl">
          <Field label="Email">
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
          </Field>
          <Field label="Password">
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          {err && <p className="rounded-lg bg-danger/10 px-3 py-2 text-xs text-danger">{err}</p>}
          <Button type="submit" className="w-full" loading={busy} size="lg">
            Sign in
          </Button>
          <p className="text-center text-[11px] text-muted">
            Demo accounts: admin@example.com / admin123 · analyst@example.com / demo1234 · viewer@example.com / demo1234
          </p>
        </form>
      </div>
    </div>
  );
}
