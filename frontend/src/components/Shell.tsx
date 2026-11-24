"use client";

import { LogOut, Menu, Moon, Sun } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { BASE, api, useApi, useMe } from "@/lib/api";
import type { Role } from "@/lib/types";
import { Loading, ToastProvider } from "./ui";

// The queue destinations, as tabs in the top bar. Counts are plain numbers beside the name.
const NAV: { href: string; label: string; roles: Role[]; count?: "exceptions" | "approvals" }[] = [
  { href: "/inbox/", label: "Inbox", roles: ["viewer", "clerk", "approver", "admin"] },
  { href: "/invoices/", label: "Invoices", roles: ["viewer", "clerk", "approver", "admin"] },
  { href: "/orders/", label: "Orders", roles: ["viewer", "clerk", "approver", "admin"] },
  { href: "/exceptions/", label: "Exceptions", roles: ["viewer", "clerk", "approver", "admin"], count: "exceptions" },
  { href: "/approvals/", label: "Approvals", roles: ["approver", "admin"], count: "approvals" },
  { href: "/vendors/", label: "Vendors", roles: ["viewer", "clerk", "approver", "admin"] },
  { href: "/evaluation/", label: "Evaluation", roles: ["viewer", "clerk", "approver", "admin"] },
  { href: "/admin/", label: "Administration", roles: ["admin"] },
];

function useTheme() {
  const [theme, setTheme] = useState("light");
  useEffect(() => setTheme(document.documentElement.dataset.theme ?? "light"), []);
  return [theme, () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* private mode */ }
    setTheme(next);
  }] as const;
}

/** The mark: a sheet with one field boxed. Drawn here, not an image. */
export function Mark() {
  return (
    <svg className="topbar__mark" viewBox="0 0 22 22" aria-hidden="true">
      <rect x="2" y="1.5" width="14" height="19" rx="2.5" fill="var(--surface)" stroke="var(--ink)" strokeWidth="1.4" />
      <rect x="5" y="6" width="8" height="3" rx="0.8" fill="var(--accent)" />
      <path d="M5 13l2.2 2.2L12 10.5" fill="none" stroke="var(--accent)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { user, signedOut, isLoading } = useMe();
  const router = useRouter();
  const path = usePathname();
  const [open, setOpen] = useState(false);
  const [theme, toggleTheme] = useTheme();
  const exceptions = useApi<{ exceptions: unknown[] }>(user ? "/api/exceptions?status=open" : null, { refreshInterval: 30_000 });
  const mine = useApi<{ invoices: unknown[] }>(user && ["approver", "admin"].includes(user.role) ? "/api/ap-approvals?mine=true" : null, { refreshInterval: 30_000 });
  const counts = { exceptions: exceptions.data?.exceptions.length ?? 0, approvals: mine.data?.invoices.length ?? 0 };

  useEffect(() => { if (signedOut) router.replace("/login/"); }, [signedOut, router]);
  useEffect(() => setOpen(false), [path]);

  if (isLoading || !user) return <Loading rows={6} label="Signing in" />;
  const signOut = async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    window.location.href = `${BASE}/login/`;
  };
  return (
    <ToastProvider>
      <div className="app">
        <header className="topbar">
          <Link className="topbar__brand" href="/inbox/">
            <Mark />
            ProcureMind AI
          </Link>
          <button className="btn btn--sm topbar__toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-controls="topnav">
            <Menu aria-hidden />
            Menu
          </button>
          <nav aria-label="Main" id="topnav" className="topbar__nav" data-open={open}>
            {NAV.filter((n) => n.roles.includes(user.role)).map(({ href, label, count }) => (
              <Link key={href} href={href} className="topbar__link" aria-current={path.startsWith(href.slice(0, -1)) ? "page" : undefined}>
                {label}
                {count && counts[count] > 0 && <span className="topbar__count">{counts[count]}</span>}
              </Link>
            ))}
          </nav>
          <div className="topbar__end">
            <span className="topbar__who">
              <span>{user.name || user.email}</span>
              <span className="muted">{user.role}</span>
            </span>
            <button className="btn btn--sm topbar__iconbtn" onClick={toggleTheme} aria-label={theme === "dark" ? "Use the light theme" : "Use the dark theme"} title={theme === "dark" ? "Light theme" : "Dark theme"}>
              {theme === "dark" ? <Sun aria-hidden /> : <Moon aria-hidden />}
            </button>
            <button className="btn btn--sm" onClick={signOut} aria-label="Sign out" title="Sign out"><LogOut aria-hidden /><span className="topbar__label">Sign out</span></button>
          </div>
        </header>
        <main>{children}</main>
      </div>
    </ToastProvider>
  );
}

/** A page: its title and what it is for, the actions on the right, then the content. `flush` drops the reading width
 * for the three-pane workspace, whose header sits on the full width. */
export function Page({ title, lede, actions, children, flush }: { title: string; lede?: string; actions?: ReactNode; children: ReactNode; flush?: boolean }) {
  return (
    <div className={flush ? "page page--flush" : "page"}>
      <header className="page__head">
        <div className="min-w-0">
          <h1>{title}</h1>
          {lede && <p className="page__lede">{lede}</p>}
        </div>
        {actions && <div className="page__actions">{actions}</div>}
      </header>
      {children}
    </div>
  );
}
