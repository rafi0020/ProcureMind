"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { amount, day, INVOICE_STATUS, when } from "@/lib/format";
import type { Exception, Vendor } from "@/lib/types";

interface Account { id: string; iban_or_account: string; bank_name: string; status: string; requested_by: string | null; decided_by: string | null; created_at: string; decided_at: string | null }
interface VendorDetail extends Vendor { aliases: string[]; bank_accounts: Account[] }
interface InvoiceRow { id: string; invoice_number: string | null; vendor_id: string | null; total: string | null; currency: string | null; status: string; invoice_date: string | null }
const ACCOUNT_CHIP: Record<string, [string, string]> = { pending: ["waiting", "waiting for a second administrator"], active: ["done", "in use"], retired: ["neutral", "replaced"], rejected: ["bad", "rejected"] };
const EXC_WORDS: Record<string, string> = { open: "open", investigating: "agent working", recommended: "recommendation ready", escalated: "escalated", resolved: "resolved" };

function NewVendor({ onDone }: { onDone: (id: string) => void }) {
  const toast = useToast();
  const [form, setForm] = useState({ name: "", tax_id: "", email: "", default_currency: "USD", payment_terms_days: "30" });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  const save = async () => {
    try {
      const v = await api<Vendor>("/api/vendors", { method: "POST", json: { ...form, tax_id: form.tax_id || null, email: form.email || null, default_currency: form.default_currency.toUpperCase(), payment_terms_days: Number(form.payment_terms_days) } });
      onDone(v.id);
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not add the vendor.", "bad"); }
  };
  return (
    <form className="panel grid gap-3 p-4 md:grid-cols-5 items-end" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <label className="grid gap-1 md:col-span-2"><span className="label">Name</span><input className="input" required value={form.name} onChange={set("name")} /></label>
      <label className="grid gap-1"><span className="label">Tax ID</span><input className="input" value={form.tax_id} onChange={set("tax_id")} /></label>
      <label className="grid gap-1"><span className="label">Currency</span><input className="input" required pattern="[A-Za-z]{3}" maxLength={3} value={form.default_currency} onChange={set("default_currency")} /></label>
      <button className="btn btn--primary">Add the vendor</button>
    </form>
  );
}

/** One vendor as a record: its facts and bank history on the sheet, its invoices and exceptions beside it. */
function VendorCase({ id, admin, userId }: { id: string; admin: boolean; userId: string | undefined }) {
  const toast = useToast();
  const v = useApi<VendorDetail>(`/api/vendors/${id}`);
  const invoices = useApi<{ invoices: InvoiceRow[] }>("/api/invoices?limit=500");
  const exceptions = useApi<{ exceptions: Exception[] }>("/api/exceptions");
  const [aliases, setAliases] = useState<string | null>(null);
  const [bank, setBank] = useState({ iban_or_account: "", bank_name: "" });
  const [requesting, setRequesting] = useState(false);
  if (v.error) return <section className="pane pane--doc"><ErrorState error={v.error} retry={() => v.mutate()} /></section>;
  if (!v.data) return <section className="pane pane--doc"><Loading rows={8} label="Loading the vendor" /></section>;
  const d = v.data;
  const show = (account: string) => (admin ? account : `ending ${account.slice(-4)}`);
  const call = async (path: string, method: string, json: unknown, done: string) => {
    try { await api(path, { method, json }); toast(done, "ok"); await v.mutate(); return true; }
    catch (e) { toast(e instanceof ApiError ? e.message : "That did not work.", "bad"); return false; }
  };
  const theirInvoices = (invoices.data?.invoices ?? []).filter((i) => i.vendor_id === id);
  const theirExceptions = (exceptions.data?.exceptions ?? []).filter((e) => e.invoice.vendor_id === id && e.status !== "resolved");
  const pending = d.bank_accounts.some((a) => a.status === "pending");

  return (
    <>
      <section className="pane pane--doc" aria-label="The vendor record">
        <article className="sheet paper" aria-label={`Vendor ${d.name}`}>
          <header className="paper__head">
            <div className="grid gap-1 min-w-0"><h2>{d.name}</h2><span className="ink-2">{[d.tax_id && `Tax ID ${d.tax_id}`, d.default_currency, `${d.payment_terms_days} days`].filter(Boolean).join(", ")}</span></div>
          </header>

          <section className="paper__section" aria-label="Names it prints">
            <h3>Names it prints</h3>
            <span className="label">Invoices with these names are matched to this vendor.</span>
            {aliases === null ? (
              <div className="flex flex-wrap gap-2 items-center">
                {d.aliases.length ? d.aliases.map((a) => <span key={a} className="chip">{a}</span>) : <span className="muted">Only its own name.</span>}
                {admin && <button className="btn btn--sm btn--quiet" onClick={() => setAliases(d.aliases.join(", "))}>Edit</button>}
              </div>
            ) : (
              <form className="flex flex-wrap gap-2" onSubmit={async (e) => { e.preventDefault(); if (await call(`/api/vendors/${id}`, "PATCH", { aliases: aliases.split(",").map((a) => a.trim()).filter(Boolean) }, "Names saved.")) setAliases(null); }}>
                <input className="input" value={aliases} onChange={(e) => setAliases(e.target.value)} aria-label="Names, separated by commas" placeholder="Names, separated by commas" autoFocus />
                <button className="btn btn--sm btn--primary">Save</button><button type="button" className="btn btn--sm btn--quiet" onClick={() => setAliases(null)}>Cancel</button>
              </form>
            )}
          </section>

          <section className="paper__section" aria-label="Bank accounts">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3>Bank accounts, newest first</h3>
              {admin && <button className="btn btn--sm" onClick={() => setRequesting((r) => !r)} aria-expanded={requesting}>Change bank details</button>}
            </div>
            {pending && <div className="banner banner--open" role="status">A bank change is waiting for a second administrator. Payments go to the account in use until it is approved.</div>}
            {requesting && (
              <form className="grid gap-3 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_auto] items-end"
                onSubmit={async (e) => { e.preventDefault(); if (await call(`/api/vendors/${id}/bank-accounts`, "POST", bank, "Requested. A second administrator must approve it before it is used.")) { setRequesting(false); setBank({ iban_or_account: "", bank_name: "" }); } }}>
                <label className="grid gap-1"><span className="label">IBAN or account number</span><input className="input" required minLength={4} value={bank.iban_or_account} onChange={(e) => setBank({ ...bank, iban_or_account: e.target.value })} /></label>
                <label className="grid gap-1"><span className="label">Bank</span><input className="input" required value={bank.bank_name} onChange={(e) => setBank({ ...bank, bank_name: e.target.value })} /></label>
                <button className="btn btn--primary">Request the change</button>
              </form>
            )}
            {d.bank_accounts.length === 0 ? <p className="muted">No bank account on file.</p> : (
              <ol className="history" aria-label="Bank account history">
                {[...d.bank_accounts].reverse().map((a) => {
                  const [chip, label] = ACCOUNT_CHIP[a.status] ?? ["neutral", a.status];
                  const canDecide = admin && a.status === "pending" && a.requested_by !== userId;
                  return (
                    <li key={a.id} className="history__item" data-state={a.status}>
                      <div className="history__row">
                        <span className="history__acct">{show(a.iban_or_account)}</span>
                        <StatusChip status={chip} label={label} />
                      </div>
                      <span className="ink-2">{a.bank_name}</span>
                      <span className="history__meta">Requested {when(a.created_at)}{a.decided_at ? `, decided ${when(a.decided_at)}` : ""}</span>
                      {canDecide && (
                        <div className="flex gap-2 mt-1">
                          <button className="btn btn--sm btn--primary" onClick={() => call(`/api/vendors/${id}/bank-accounts/${a.id}/decide`, "POST", { decision: "approved" }, "Approved; payments now go to this account.")}>Approve</button>
                          <button className="btn btn--sm" onClick={() => call(`/api/vendors/${id}/bank-accounts/${a.id}/decide`, "POST", { decision: "rejected" }, "Rejected.")}>Reject</button>
                        </div>
                      )}
                      {a.status === "pending" && admin && !canDecide && <span className="hint">You requested it; another administrator decides.</span>}
                    </li>
                  );
                })}
              </ol>
            )}
          </section>
        </article>
      </section>

      <section className="pane pane--side" aria-label="Invoices and exceptions for this vendor">
        <div className="decide">
          <section className="grid gap-2" aria-label="Open exceptions">
            <h2>Open exceptions</h2>
            {theirExceptions.length === 0 ? <p className="muted">None open.</p> : (
              <ul className="queue well">
                {theirExceptions.map((e) => (
                  <li key={e.id}><Link className="queue__item" href={`/exceptions/?id=${e.id}`}>
                    <span className="queue__top"><span className="queue__ref">{e.invoice.number ?? "no number"}</span><StatusChip status={e.status === "escalated" ? "bad" : "waiting"} label={EXC_WORDS[e.status]} /></span>
                    <span className="queue__sub"><span>{e.reasons[0]?.code.replace(/_/g, " ") ?? "held"}</span><span className="queue__amt">{amount(e.invoice.total, e.invoice.currency)}</span></span>
                  </Link></li>
                ))}
              </ul>
            )}
          </section>
          <section className="grid gap-2" aria-label="Invoices from this vendor">
            <h2>Invoices</h2>
            {invoices.isLoading && <Loading rows={3} />}
            {!invoices.isLoading && theirInvoices.length === 0 && <p className="muted">No invoices from this vendor yet.</p>}
            {!!theirInvoices.length && (
              <ul className="queue well">
                {theirInvoices.map((i) => (
                  <li key={i.id}><Link className="queue__item" href={`/invoices/?id=${i.id}`}>
                    <span className="queue__top"><span className="queue__ref">{i.invoice_number ?? "no number"}</span><span className="queue__amt">{amount(i.total, i.currency)}</span></span>
                    <span className="queue__sub"><span>{i.invoice_date ? day(i.invoice_date) : ""}</span><StatusChip status={i.status} label={INVOICE_STATUS[i.status]} /></span>
                  </Link></li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </section>
    </>
  );
}

function VendorsRoute() {
  const id = useSearchParams().get("id");
  const router = useRouter();
  const { user } = useMe();
  const list = useApi<{ vendors: Vendor[] }>("/api/vendors");
  const [adding, setAdding] = useState(false);
  const admin = user?.role === "admin";
  return (
    <Page flush title="Vendors"
      lede="Who you buy from, the names they print, and where they are paid. Bank details change only with two administrators."
      actions={id ? <Link className="btn btn--sm" href="/vendors/">All vendors</Link> : admin ? <button className="btn btn--sm btn--primary" onClick={() => setAdding((a) => !a)} aria-expanded={adding}>Add a vendor</button> : undefined}>
      {adding && !id && <div className="bar-note"><NewVendor onDone={(vid) => router.push(`/vendors/?id=${vid}`)} /></div>}
      {list.error && <div className="bar-note"><ErrorState error={list.error} retry={() => list.mutate()} /></div>}
      <div className="workspace" data-picked={id ? "true" : "false"}>
        <section className="pane pane--list" aria-label="Vendors">
          {list.isLoading && <Loading rows={6} label="Loading the vendors" />}
          {list.data && list.data.vendors.length === 0 && <Empty title="No vendors yet" />}
          {!!list.data?.vendors.length && (
            <ul className="queue">
              {list.data.vendors.map((v) => (
                <li key={v.id}>
                  <Link className="queue__item" href={`/vendors/?id=${v.id}`} aria-current={v.id === id ? "true" : undefined}>
                    <span className="queue__top"><span className="queue__name">{v.name}</span><span className="queue__age">{v.default_currency}</span></span>
                    <span className="queue__sub"><span>{v.tax_id ?? "no tax ID"}</span><span className="queue__age">{v.payment_terms_days} days</span></span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
        {id ? (
          <VendorCase key={id} id={id} admin={admin} userId={user?.id} />
        ) : (
          <section className="pane pane--idle" aria-label="Choose a vendor">
            <strong>Choose a vendor from the list.</strong>
            <span>Its record, names and bank history appear on the sheet; its invoices and exceptions beside it.</span>
          </section>
        )}
      </div>
    </Page>
  );
}

export default function Vendors() {
  return <Suspense fallback={<Loading />}><VendorsRoute /></Suspense>;
}
