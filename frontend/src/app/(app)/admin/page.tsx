"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { when } from "@/lib/format";
import type { Role } from "@/lib/types";

interface Member { id: string; email: string; name: string; role: Role; is_active: boolean; created_at: string }
interface LLM { provider: string; model: string; base_url: string; fallback_model: string; price_input_per_mtok: number; price_output_per_mtok: number; api_key_set: boolean; source: string }
interface AuditEntry { id: number; at: string; user: string | null; action: string; target: string | null; detail: Record<string, unknown> | null; ip: string | null }
interface Tier { up_to: string | null; approvals: number; role: "approver" | "admin" }
interface Rules { price_tolerance_pct: string; qty_tolerance_pct: string; total_tolerance_abs: string; approval_tiers: Tier[]; escalation_hours: number }
interface InboxKey { id: string; name: string; prefix: string; created_at: string; revoked_at: string | null }

const SECTIONS = [
  ["users", "People and roles", "Who can sign in, and what each role may do."],
  ["rules", "Matching and approval rules", "Tolerances, approval tiers and when an overdue invoice escalates."],
  ["inbox", "Inbox keys", "Keys for mail forwarders that post attachments to the inbox."],
  ["model", "Model", "The model the Exception Agent uses, and what it costs."],
  ["audit", "Audit log", "A record of who did what, and when."],
] as const;
const message = (e: unknown) => (e instanceof ApiError ? e.message : "That did not work.");

/** A setting: its control on the left, what it does and why on the right. */
function Setting({ title, explain, children, htmlFor }: { title: string; explain: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="setting">
      <div className="setting__control">
        <span className="label">{htmlFor ? <label htmlFor={htmlFor}>{title}</label> : title}</span>
        {children}
      </div>
      <div className="setting__explain">{explain}</div>
    </div>
  );
}

function Users() {
  const { user: me } = useMe();
  const toast = useToast();
  const { data, error, isLoading, mutate } = useApi<{ users: Member[] }>("/api/users");
  const [form, setForm] = useState({ email: "", name: "", role: "clerk" as Role, password: "" });
  const [problem, setProblem] = useState("");
  const patch = async (id: string, json: Partial<Member>, done: string) => {
    try { await api(`/api/users/${id}`, { method: "PATCH", json }); toast(done); } catch (e) { toast(message(e), "bad"); }
    await mutate();
  };
  const add = async (event: FormEvent) => {
    event.preventDefault();
    setProblem("");
    try {
      await api("/api/users", { method: "POST", json: form });
      toast(`${form.email} can now sign in.`);
      setForm({ email: "", name: "", role: "clerk", password: "" });
      await mutate();
    } catch (e) { setProblem(message(e)); }
  };
  return (
    <div className="grid gap-5 min-w-0">
      <div className="setting-sheet">
        <header className="setting-sheet__head"><h2>People</h2><span className="ink-2">Change a role or deactivate someone here. A deactivated person is signed out at once.</span></header>
        <div className="table-wrap">
          {isLoading && <Loading />}
          {error && <div className="p-4"><ErrorState error={error} retry={() => mutate()} /></div>}
          {data && (
            <table className="table table--stack">
              <thead><tr><th>Person</th><th>Role</th><th>Status</th><th>Added</th><th /></tr></thead>
              <tbody>
                {data.users.map((u) => (
                  <tr key={u.id}>
                    <td data-label="Person"><span>{u.name}</span><div className="label measured">{u.email}</div></td>
                    <td data-label="Role">
                      <select className="select" aria-label={`Role of ${u.email}`} value={u.role} onChange={(e) => patch(u.id, { role: e.target.value as Role }, `${u.email} is now ${e.target.value}.`)}>
                        <option value="viewer">viewer</option><option value="clerk">clerk</option><option value="approver">approver</option><option value="admin">admin</option>
                      </select>
                    </td>
                    <td data-label="Status"><StatusChip status={u.is_active ? "active" : "rejected"} label={u.is_active ? "active" : "deactivated"} /></td>
                    <td data-label="Added" className="muted">{when(u.created_at)}</td>
                    <td data-label="">
                      {u.id !== me?.id && <button className={`btn btn--sm ${u.is_active ? "btn--danger" : ""}`} onClick={() => patch(u.id, { is_active: !u.is_active }, u.is_active ? `${u.email} was signed out and deactivated.` : `${u.email} can sign in again.`)}>{u.is_active ? "Deactivate" : "Reactivate"}</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
      <form className="setting-sheet" onSubmit={add} aria-label="Add a person">
        <header className="setting-sheet__head"><h2>Add a person</h2><span className="ink-2">They sign in with the e-mail and password given here.</span></header>
        <Setting title="Name" htmlFor="new-name" explain="As it should appear in the top bar and on the records they touch.">
          <input id="new-name" className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Setting>
        <Setting title="E-mail" htmlFor="new-email" explain="Their sign-in. It must be unique in this organisation.">
          <input id="new-email" className="input" type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </Setting>
        <Setting title="Role" htmlFor="new-role" explain={<><strong>Viewer</strong> reads. <strong>Clerk</strong> uploads, corrects, matches and asks the agent. <strong>Approver</strong> approves invoices and decides exceptions. <strong>Administrator</strong> does all of that and manages people, vendors and rules.</>}>
          <select id="new-role" className="select" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
            <option value="viewer">viewer: reads</option><option value="clerk">clerk: uploads, corrects, matches, asks the agent</option><option value="approver">approver: approves invoices, decides exceptions</option><option value="admin">admin: all of that, and manages people, vendors and rules</option>
          </select>
        </Setting>
        <Setting title="Initial password" htmlFor="new-password" explain="They should change it at first sign-in. Share it through your password manager, not by e-mail.">
          <input id="new-password" className="input" type="password" required autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <span className="hint">At least 10 characters, with a letter and a number.</span>
        </Setting>
        {problem && <p className="hint hint--bad px-5 pb-4" role="alert">{problem}</p>}
        <div className="setting__actions"><button className="btn btn--primary">Add person</button></div>
      </form>
    </div>
  );
}

function Model() {
  const toast = useToast();
  const { data, error, isLoading, mutate } = useApi<LLM>("/api/settings/llm");
  const [form, setForm] = useState<LLM | null>(null);
  const [key, setKey] = useState("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (data) setForm(data); }, [data]);
  if (isLoading || !form) return error ? <ErrorState error={error} retry={() => mutate()} /> : <div className="setting-sheet"><Loading /></div>;
  const set = <K extends keyof LLM>(k: K, v: LLM[K]) => setForm({ ...form, [k]: v });
  const save = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const body = { ...form, api_key: key ? key : undefined };
      await mutate(await api<LLM>("/api/settings/llm", { method: "PUT", json: body }), false);
      setKey("");
      toast("Model settings saved.");
    } catch (e) { toast(message(e), "bad"); }
  };
  const test = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api<{ ok: boolean; error?: string; model?: string; latency_ms?: number }>("/api/settings/llm/test", { method: "POST" });
      setResult(r.ok ? { ok: true, text: `Connected to ${r.model} in ${r.latency_ms} ms.` } : { ok: false, text: r.error ?? "The test failed." });
    } catch (e) { setResult({ ok: false, text: message(e) }); }
    setBusy(false);
  };
  return (
    <form className="setting-sheet" onSubmit={save} aria-label="Model provider">
      <header className="setting-sheet__head"><h2>Model provider</h2><span className="ink-2">{form.source === "organisation" ? "Set for this organisation." : "Using the server default until you save here."}</span></header>
      <Setting title="Provider" htmlFor="m-provider" explain="The kind of service the agent calls. Use an OpenAI-compatible address for most gateways; Ollama runs on your own machine.">
        <select id="m-provider" className="select" value={form.provider} onChange={(e) => set("provider", e.target.value)}>
          <option value="openai_compat">OpenAI-compatible (OpenAI, Gemini, Groq, 9Router, any gateway)</option>
          <option value="anthropic">Anthropic</option><option value="ollama">Ollama (local)</option><option value="stub">Built-in demonstration script (no model)</option>
        </select>
      </Setting>
      <Setting title="Model" htmlFor="m-model" explain="The exact name your provider uses for the model, as its documentation writes it.">
        <input id="m-model" className="input measured" required value={form.model} onChange={(e) => set("model", e.target.value)} />
      </Setting>
      <Setting title="Base URL" htmlFor="m-url" explain="Where the provider answers. Leave it empty for the provider's own address.">
        <input id="m-url" className="input measured" value={form.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder={form.provider === "ollama" ? "http://host.docker.internal:11434/v1" : "https://api.example.com/v1"} />
      </Setting>
      <Setting title="Fallback model (optional)" htmlFor="m-fallback" explain="Used when the main model is unavailable or refuses a call. Leave it empty to fail instead.">
        <input id="m-fallback" className="input measured" value={form.fallback_model} onChange={(e) => set("fallback_model", e.target.value)} />
      </Setting>
      <Setting title="API key" htmlFor="m-key" explain="Stored encrypted. It is never shown again and never sent to the browser. Leave it blank to keep the stored key.">
        <input id="m-key" className="input measured" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder={form.api_key_set ? "A key is stored. Type a new one to replace it." : "No key stored"} />
      </Setting>
      <Setting title="Price per million input tokens, USD" htmlFor="m-pin" explain="Used to estimate each run's cost and to enforce budgets. Set it to what your provider charges.">
        <input id="m-pin" className="input measured" type="number" min={0} step="0.01" value={form.price_input_per_mtok} onChange={(e) => set("price_input_per_mtok", Number(e.target.value))} />
      </Setting>
      <Setting title="Price per million output tokens, USD" htmlFor="m-pout" explain="The same, for the tokens the model writes back.">
        <input id="m-pout" className="input measured" type="number" min={0} step="0.01" value={form.price_output_per_mtok} onChange={(e) => set("price_output_per_mtok", Number(e.target.value))} />
      </Setting>
      <div className="setting__actions">
        <button className="btn btn--primary">Save</button>
        <button type="button" className="btn" onClick={test} disabled={busy} aria-busy={busy}>Test the saved settings</button>
        {result && <span className={result.ok ? "ok" : "bad"} role="status">{result.text}</span>}
      </div>
    </form>
  );
}

function Audit() {
  const [action, setAction] = useState("");
  const [before, setBefore] = useState<number | null>(null);
  const { data, error, isLoading, mutate } = useApi<{ entries: AuditEntry[]; next_before: number | null }>(`/api/audit?limit=50&action=${encodeURIComponent(action)}${before ? `&before=${before}` : ""}`, { keepPreviousData: true });
  return (
    <div className="grid gap-5 min-w-0">
      <div className="flex flex-wrap items-end gap-2">
        <label className="field max-w-xs"><span className="label">Show</span>
          <select className="select" value={action} onChange={(e) => { setAction(e.target.value); setBefore(null); }} aria-label="Filter by kind">
            <option value="">Everything</option><option value="auth.">Sign-ins</option><option value="user.">People</option><option value="invoice.">Invoices</option><option value="order.">Orders</option><option value="exception.">Exceptions</option><option value="vendor.">Vendors</option><option value="inbox_key.">Inbox keys</option><option value="settings.">Settings</option>
          </select>
        </label>
        {before && <button className="btn" onClick={() => setBefore(null)}>Back to newest</button>}
      </div>
      <div className="setting-sheet">
        <div className="table-wrap">
          {isLoading && !data && <Loading />}
          {error && <div className="p-4"><ErrorState error={error} retry={() => mutate()} /></div>}
          {data?.entries.length === 0 && <Empty title="No entries">Nothing of this kind has been recorded.</Empty>}
          {!!data?.entries.length && (
            <table className="table table--stack">
              <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th><th>Address</th></tr></thead>
              <tbody>
                {data.entries.map((e) => (
                  <tr key={e.id}>
                    <td data-label="When" className="muted measured whitespace-nowrap">{when(e.at)}</td><td data-label="Who">{e.user ?? "deleted user"}</td><td data-label="Action" className="measured">{e.action}</td>
                    <td data-label="Detail" className="ink-2 measured">{e.detail ? JSON.stringify(e.detail).slice(0, 160) : ""}</td><td data-label="Address" className="muted measured">{e.ip}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
      {data?.next_before && <div><button className="btn" onClick={() => setBefore(data.next_before)}>Older entries</button></div>}
    </div>
  );
}

function ApRules() {
  const toast = useToast();
  const { data, error, mutate } = useApi<Rules>("/api/ap-settings");
  const [form, setForm] = useState<Rules | null>(null);
  useEffect(() => { if (data) setForm({ ...data, approval_tiers: data.approval_tiers.map((t) => ({ ...t, up_to: t.up_to == null ? null : String(t.up_to) })) }); }, [data]);
  if (!form) return error ? <ErrorState error={error} retry={() => mutate()} /> : <div className="setting-sheet"><Loading /></div>;
  const tier = (i: number, patch: Partial<Tier>) => setForm({ ...form, approval_tiers: form.approval_tiers.map((t, j) => (j === i ? { ...t, ...patch } : t)) });
  const save = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await mutate(await api<Rules>("/api/ap-settings", { method: "PUT", json: { ...form, approval_tiers: form.approval_tiers.map((t, i) => ({ ...t, up_to: i === form.approval_tiers.length - 1 ? null : t.up_to })) } }), false);
      toast("Rules saved. They apply to invoices matched from now on.");
    } catch (e) { toast(message(e), "bad"); }
  };
  return (
    <form className="grid gap-5 min-w-0" onSubmit={save} aria-label="Matching and approval rules">
      <div className="setting-sheet">
        <header className="setting-sheet__head"><h2>Matching tolerances</h2><span className="ink-2">Differences inside these limits are allowed; outside them an invoice is held as an exception.</span></header>
        <Setting title="Unit price above the PO, %" htmlFor="r-price" explain="A line whose unit price is more than this percentage above the purchase order's price is held for a person to decide.">
          <input id="r-price" className="input measured" type="number" min={0} max={100} step="0.01" value={form.price_tolerance_pct} onChange={(e) => setForm({ ...form, price_tolerance_pct: e.target.value })} />
        </Setting>
        <Setting title="Quantity above received, %" htmlFor="r-qty" explain="Quantity billed may exceed what was received by this much before the invoice is held. Zero allows no excess.">
          <input id="r-qty" className="input measured" type="number" min={0} max={100} step="0.01" value={form.qty_tolerance_pct} onChange={(e) => setForm({ ...form, qty_tolerance_pct: e.target.value })} />
        </Setting>
        <Setting title="Header totals, absolute" htmlFor="r-total" explain="The difference allowed between an invoice's total and what the lines add up to, in the invoice's currency. Rounding is the usual reason to allow a little.">
          <input id="r-total" className="input measured" type="number" min={0} step="0.01" value={form.total_tolerance_abs} onChange={(e) => setForm({ ...form, total_tolerance_abs: e.target.value })} />
        </Setting>
      </div>

      <div className="setting-sheet">
        <header className="setting-sheet__head"><h2>Approval tiers</h2><span className="ink-2">By invoice total. An invoice needs the approvals of the first tier its total fits under. The last tier has no upper limit.</span></header>
        {form.approval_tiers.map((t, i) => {
          const last = i === form.approval_tiers.length - 1;
          return (
            <div key={i} className="setting">
              <div className="setting__control">
                <span className="label">Tier {i + 1}</span>
                <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] items-end">
                  <label className="field"><span className="label">Up to</span>{last ? <input className="input" disabled value="no limit" aria-label={`Tier ${i + 1} up to`} /> : <input className="input measured" type="number" min={0.01} step="0.01" required value={t.up_to ?? ""} aria-label={`Tier ${i + 1} up to`} onChange={(e) => tier(i, { up_to: e.target.value })} />}</label>
                  <label className="field"><span className="label">Approvals needed</span><input className="input" type="number" min={1} max={5} value={t.approvals} aria-label={`Tier ${i + 1} approvals needed`} onChange={(e) => tier(i, { approvals: Number(e.target.value) })} /></label>
                  <label className="field"><span className="label">At least one</span>
                    <select className="select" value={t.role} aria-label={`Tier ${i + 1} at least one`} onChange={(e) => tier(i, { role: e.target.value as Tier["role"] })}><option value="approver">approver</option><option value="admin">administrator</option></select>
                  </label>
                </div>
              </div>
              <div className="setting__explain">
                <span>{last ? "Invoices above the tier before." : `Invoices up to ${t.up_to || "a limit you set"} need ${t.approvals} approval${t.approvals === 1 ? "" : "s"}, at least one from ${t.role === "admin" ? "an administrator" : "an approver"}.`}</span>
                <button type="button" className="btn btn--sm btn--quiet justify-self-start" disabled={form.approval_tiers.length === 1} onClick={() => setForm({ ...form, approval_tiers: form.approval_tiers.filter((_, j) => j !== i) })}>Remove this tier</button>
              </div>
            </div>
          );
        })}
        <div className="setting__actions">
          <button type="button" className="btn btn--sm" onClick={() => setForm({ ...form, approval_tiers: [...form.approval_tiers.slice(0, -1), { up_to: "", approvals: 1, role: "approver" }, form.approval_tiers[form.approval_tiers.length - 1]] })}>Add a tier</button>
        </div>
      </div>

      <div className="setting-sheet">
        <header className="setting-sheet__head"><h2>Escalation</h2></header>
        <Setting title="Escalate after, hours" htmlFor="r-esc" explain="An invoice still waiting for approval after this many hours is marked overdue and listed first for approvers.">
          <input id="r-esc" className="input measured" type="number" min={1} max={720} value={form.escalation_hours} onChange={(e) => setForm({ ...form, escalation_hours: Number(e.target.value) })} />
        </Setting>
        <div className="setting__actions"><button className="btn btn--primary">Save the rules</button></div>
      </div>
    </form>
  );
}

function InboxKeys() {
  const toast = useToast();
  const { data, error, mutate } = useApi<{ keys: InboxKey[] }>("/api/inbox-keys");
  const [name, setName] = useState("");
  const [shown, setShown] = useState<string | null>(null);
  const create = async (event: FormEvent) => {
    event.preventDefault();
    try { const k = await api<InboxKey & { key: string }>("/api/inbox-keys", { method: "POST", json: { name } }); setShown(k.key); setName(""); await mutate(); }
    catch (e) { toast(message(e), "bad"); }
  };
  return (
    <div className="grid gap-5 min-w-0">
      <form className="setting-sheet" onSubmit={create} aria-label="A key for a mail forwarder">
        <header className="setting-sheet__head"><h2>A key for a mail forwarder</h2><span className="ink-2">Your mail system posts attachments to /v1/inbox with this key.</span></header>
        <Setting title="Name" htmlFor="k-name" explain="A label for the forwarder, so the key can be told apart and revoked later.">
          <input id="k-name" className="input" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} placeholder="ap@ forwarder" />
        </Setting>
        {shown && <div className="banner banner--open m-5" role="status"><div className="grid gap-1"><strong>Copy it now. It is not shown again.</strong><code className="measured" style={{ overflowWrap: "anywhere" }}>{shown}</code></div>
          <button type="button" className="btn btn--sm" onClick={() => { void navigator.clipboard?.writeText(shown); toast("Copied."); }}>Copy</button></div>}
        <div className="setting__actions"><button className="btn btn--primary">Create the key</button></div>
      </form>
      <div className="setting-sheet">
        <div className="table-wrap">
          {error && <div className="p-4"><ErrorState error={error} retry={() => mutate()} /></div>}
          {data?.keys.length === 0 && <Empty title="No keys">Create one above.</Empty>}
          {!!data?.keys.length && (
            <table className="table table--stack">
              <thead><tr><th>Name</th><th>Starts with</th><th>Created</th><th /></tr></thead>
              <tbody>{data.keys.map((k) => (
                <tr key={k.id}><td data-label="Name">{k.name}</td><td data-label="Starts with" className="measured">{k.prefix}</td><td data-label="Created" className="muted">{when(k.created_at)}</td>
                  <td data-label="">{k.revoked_at ? <span className="chip">revoked</span> : <button className="btn btn--sm btn--danger" onClick={async () => { if (confirm(`Revoke ${k.name}? Mail forwarded with it is refused at once.`)) { try { await api(`/api/inbox-keys/${k.id}`, { method: "DELETE" }); toast("Revoked."); await mutate(); } catch (e) { toast(message(e), "bad"); } } }}>Revoke</button>}</td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

function AdminPage() {
  const tab = useSearchParams().get("tab") ?? "users";
  const { user } = useMe();
  if (user && user.role !== "admin") return <Page title="Administration"><div className="panel"><Empty title="Administrators only">Ask an administrator of your organisation for access.</Empty></div></Page>;
  const current = SECTIONS.find(([v]) => v === tab) ?? SECTIONS[0];
  return (
    <Page title="Administration" lede="People and roles, the rules invoices are matched and approved by, inbox keys, the model the Exception Agent uses, and the record of who did what.">
      <div className="settings">
        <nav className="settings__nav" aria-label="Administration sections">
          {SECTIONS.map(([value, label]) => (
            <Link key={value} href={`/admin/?tab=${value}`} aria-current={current[0] === value ? "page" : undefined}>{label}</Link>
          ))}
        </nav>
        <div className="settings__body min-w-0">
          <header className="grid gap-1">
            <h2>{current[1]}</h2>
            <span className="ink-2">{current[2]}</span>
          </header>
          {tab === "rules" ? <ApRules /> : tab === "inbox" ? <InboxKeys /> : tab === "model" ? <Model /> : tab === "audit" ? <Audit /> : <Users />}
        </div>
      </div>
    </Page>
  );
}

export default function AdminRoute() {
  return (<Suspense fallback={<Loading />}><AdminPage /></Suspense>);
}
