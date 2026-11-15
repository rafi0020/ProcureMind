"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { api, ApiError, setCsrf } from "@/lib/api";
import { Mark } from "./Shell";

// The demo accounts of this installation (the seed in backend/seed). Public demo credentials on a local app.
const DEMO = [
  ["Clerk", "clerk@kestrel.demo", "Clerk#Demo2026"],
  ["Approver", "approver@kestrel.demo", "Approver#Demo2026"],
  ["Administrator", "admin@kestrel.demo", "Admin#Demo2026"],
  ["Viewer", "viewer@kestrel.demo", "Viewer#Demo2026"],
];

/** A sample supplier invoice drawn in HTML and CSS: decorative, demo values, no real company. */
function Specimen() {
  return (
    <div className="specimen" aria-hidden="true">
      <div className="specimen__head">
        <div>
          <div className="specimen__name">Sample Supplier Ltd</div>
          <div className="specimen__bill">12 Example Road, Demoville</div>
        </div>
        <div className="specimen__meta">
          <div><span className="specimen__hit">Invoice INV-0412</span></div>
          <div>10 Nov 2026</div>
          <div>PO PO-1042</div>
        </div>
      </div>
      <div className="specimen__bill">
        <span>Bill to</span>
        <span>Kestrel Foods (demo)</span>
      </div>
      <table>
        <thead><tr><th>Item</th><th className="n">Qty</th><th className="n">Price</th><th className="n">Amount</th></tr></thead>
        <tbody>
          <tr><td>Copier paper A4</td><td className="n">10</td><td className="n">45.00</td><td className="n">450.00</td></tr>
          <tr><td>Toner cartridge</td><td className="n">2</td><td className="n">60.00</td><td className="n">120.00</td></tr>
          <tr><td>Printer drum</td><td className="n">1</td><td className="n">80.00</td><td className="n">80.00</td></tr>
          <tr><td>Desk tidy</td><td className="n">1</td><td className="n">12.00</td><td className="n">12.00</td></tr>
          <tr><td>Delivery</td><td className="n">1</td><td className="n">18.00</td><td className="n">18.00</td></tr>
        </tbody>
      </table>
      <div className="specimen__sums">
        <div><span>Subtotal</span><span>680.00</span></div>
        <div><span>VAT 20%</span><span>136.00</span></div>
      </div>
      <div className="specimen__total"><span>Total USD</span><span className="specimen__hit"><b>816.00</b></span></div>
      <div className="specimen__foot">
        <span className="specimen__ok">Matched to PO-1042, 5 of 5 lines</span>
        <span>Demo values</span>
      </div>
    </div>
  );
}

export function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const [form, setForm] = useState({ email: "", password: "", name: "", org_name: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = mode === "login" ? { email: form.email, password: form.password } : form;
      const result = await api<{ csrf_token: string }>(`/api/auth/${mode}`, { method: "POST", json: body });
      setCsrf(result.csrf_token);
      window.location.href = "/inbox/";
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong.");
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="auth__form">
        <div className="auth__form-inner">
          <Link className="auth__brand" href="/"><Mark />ProcureMind AI</Link>
          <div className="grid gap-2">
            <h1>{mode === "login" ? "Sign in" : "Create your organisation"}</h1>
            <p className="muted">{mode === "login" ? "Your queue is waiting." : "You become its first administrator."}</p>
          </div>
          <form className="grid gap-4" onSubmit={submit} noValidate>
            {mode === "signup" && (
              <>
                <label className="field"><span className="label">Organisation name</span><input className="input" required value={form.org_name} onChange={set("org_name")} autoComplete="organization" /></label>
                <label className="field"><span className="label">Your name</span><input className="input" required value={form.name} onChange={set("name")} autoComplete="name" /></label>
              </>
            )}
            <label className="field"><span className="label">E-mail</span><input className="input" type="email" required value={form.email} onChange={set("email")} autoComplete="email" aria-invalid={!!error} /></label>
            <label className="field">
              <span className="label">Password</span>
              <input className="input" type="password" required value={form.password} onChange={set("password")} autoComplete={mode === "login" ? "current-password" : "new-password"} aria-invalid={!!error} />
              {mode === "signup" && <span className="hint">At least 10 characters, with a letter and a number.</span>}
            </label>
            {error && <p className="hint hint--bad" role="alert">{error}</p>}
            <button className="btn btn--primary btn--block" disabled={busy} aria-busy={busy}>{mode === "login" ? "Sign in" : "Create organisation"}</button>
          </form>
          <p className="muted">
            {mode === "login" ? <>New here? <Link href="/signup/">Create an organisation</Link></> : <>Already have an account? <Link href="/login/">Sign in</Link></>}
          </p>
          {mode === "login" && (
            <section className="demo" aria-label="Demo accounts">
              <h2>Demo accounts</h2>
              <p className="hint">Generated data for Kestrel Foods. Public credentials on a local installation.</p>
              <table className="table">
                <tbody>
                  {DEMO.map(([role, email, password]) => (
                    <tr key={email}>
                      <td>{role}</td>
                      <td className="measured">{email}</td>
                      <td className="text-right"><button type="button" className="btn btn--sm btn--quiet" onClick={() => setForm({ ...form, email, password })}>Fill in</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </div>
      </div>
      <aside className="auth__aside">
        <Specimen />
        <div className="auth__aside-copy">
          <p className="ink-2 font-medium">Invoices read, matched and checked. Exceptions looked into by an agent that cannot change anything. Approvals decided by amount.</p>
          <p className="hint">Nothing is paid from here.</p>
        </div>
      </aside>
    </div>
  );
}
