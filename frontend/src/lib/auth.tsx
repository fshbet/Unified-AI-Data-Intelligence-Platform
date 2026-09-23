"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, getToken } from "./api";

export type User = { id: string; email: string; name: string; role: "admin" | "analyst" | "viewer"; department?: string | null };

type Ctx = { user: User | null; loading: boolean; login: (email: string, password: string) => Promise<void>; logout: () => void; refresh: () => Promise<void> };
const AuthContext = createContext<Ctx>({ user: null, loading: true, login: async () => {}, logout: () => {}, refresh: async () => {} });

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      setUser(await api<User>("/auth/me"));
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const login = async (email: string, password: string) => {
    const r = await api<{ token: string; user: User }>("/auth/login", { method: "POST", json: { email, password } });
    localStorage.setItem("edi_token", r.token);
    setUser(r.user);
  };
  const logout = () => {
    localStorage.removeItem("edi_token");
    setUser(null);
    window.location.href = "/login";
  };
  return <AuthContext.Provider value={{ user, loading, login, logout, refresh }}>{children}</AuthContext.Provider>;
}

export const useAuth = () => useContext(AuthContext);

export function useTheme() {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const stored = localStorage.getItem("edi_theme");
    const d = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    setDark(d);
    document.documentElement.classList.toggle("dark", d);
  }, []);
  const toggle = () => {
    const d = !dark;
    setDark(d);
    localStorage.setItem("edi_theme", d ? "dark" : "light");
    document.documentElement.classList.toggle("dark", d);
  };
  return { dark, toggle };
}
