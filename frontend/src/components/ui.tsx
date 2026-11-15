"use client";

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import type { ApiError } from "@/lib/api";

/** A skeleton in the shape of what is coming: rows of the table or the lines of the page. */
export function Loading({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="grid gap-3 p-5" role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height: 12, width: `${88 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

/** What belongs here, and the one action that fills it. */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

/** What broke, and what to do. */
export function ErrorState({ error, retry }: { error: ApiError | Error; retry?: () => void }) {
  return (
    <div className="banner banner--bad m-0" role="alert">
      <span>{error.message}</span>
      {retry && (
        <button className="btn btn--sm" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}

type Toast = { id: number; text: string; kind: "ok" | "bad" };
const ToastContext = createContext<(text: string, kind?: "ok" | "bad") => void>(() => {});
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: "ok" | "bad" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast" data-kind={t.kind} role={t.kind === "bad" ? "alert" : "status"}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// Green is done or matched; amber is open, waiting or needs a person; red is an exception or a failure.
const CHIP: Record<string, string> = {
  completed: "chip--matched", approved: "chip--matched", active: "chip--matched", extracted: "chip--matched", matched: "chip--matched",
  resolved: "chip--matched", ready: "chip--matched", done: "chip--matched",
  pending_approval: "chip--open", needs_review: "chip--open", on_hold: "chip--open", open: "chip--open", recommended: "chip--open",
  investigating: "chip--open", pending: "chip--open", waiting_user: "chip--open", waiting_approval: "chip--open", queued: "chip--open",
  processing: "chip--open", running: "chip--open", waiting: "chip--open",
  exception: "chip--bad", rejected: "chip--bad", dead: "chip--bad", failed: "chip--bad", escalated: "chip--bad", budget_exceeded: "chip--bad", bad: "chip--bad",
  neutral: "chip--neutral", deactivated: "chip--neutral",
};
export function StatusChip({ status, label }: { status: string; label?: string }) {
  return <span className={`chip ${CHIP[status] ?? ""}`}>{label ?? status.replace(/_/g, " ")}</span>;
}

export function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <>
      <button className="drawer-scrim" aria-label="Close" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={title} onKeyDown={(e) => e.key === "Escape" && onClose()}>
        <div className="panel__head">
          <h2>{title}</h2>
          <button className="btn btn--sm" onClick={onClose} autoFocus>
            Close
          </button>
        </div>
        <div className="drawer__body">{children}</div>
      </aside>
    </>
  );
}
