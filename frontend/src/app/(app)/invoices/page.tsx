"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { MatchTable } from "@/components/MatchTable";
import { PageViewer } from "@/components/PageViewer";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { amount, day, INVOICE_STATUS } from "@/lib/format";
import type { Exception, Invoice, InvoiceLine, Provenance, Vendor } from "@/lib/types";

const EDITABLE = ["needs_review", "ready", "exception", "matched", "on_hold"];
const FILTERS: [string, string][] = [["", "All"], ["needs_review", "Needs review"], ["exception", "Exceptions"], ["pending_approval", "Waiting for approval"], ["approved", "Approved"], ["on_hold", "On hold"]];
// Extraction names its fields after the document; the invoice after its columns.
const FIELDS: { key: keyof Invoice; label: string; from: string; input?: string }[] = [
  { key: "invoice_number", label: "Invoice number", from: "doc_number" }, { key: "invoice_date", label: "Invoice date", from: "doc_date", input: "date" },
  { key: "due_date", label: "Due date", from: "due_date", input: "date" }, { key: "vendor_name_printed", label: "Vendor as printed", from: "vendor_name" },
  { key: "po_number", label: "PO number", from: "po_number" }, { key: "currency", label: "Currency", from: "currency" },
  { key: "subtotal", label: "Subtotal", from: "subtotal" }, { key: "tax", label: "Tax", from: "tax" }, { key: "total", label: "Total", from: "total" },
  { key: "bank_account", label: "Bank account", from: "bank_account" },
];
const FIGURES = new Set(["invoice_number", "po_number", "subtotal", "tax", "total", "bank_account"]);

interface InvoiceRow { id: string; kind: string; invoice_number: string | null; vendor_id: string | null; vendor_name_printed: string | null; invoice_date: string | null; total: string | null; currency: string | null; status: string }

/** The list of invoices. On its own it is a full table; beside a case it is the narrow pane on the left. */
function useInvoiceList(status: string) {
  const list = useApi<{ invoices: InvoiceRow[] }>(`/api/invoices?limit=500${status ? `&status=${status}` : ""}`);
  const vendors = useApi<{ vendors: Vendor[] }>("/api/vendors");
  const names = new Map((vendors.data?.vendors ?? []).map((v) => [v.id, v.name]));
  return { list, names };
}

function InvoiceQueue() {
  const router = useRouter();
  const [status, setStatus] = useState("");
  const { list, names } = useInvoiceList(status);
  return (
    <Page title="Invoices" lede="Every invoice and credit note: what was read from it, how it matched, and where it stands.">
      <div className="tabs" role="tablist" aria-label="Filter">
        {FILTERS.map(([value, label]) => <button key={value} role="tab" className="tab" aria-selected={status === value} onClick={() => setStatus(value)}>{label}</button>)}
      </div>
      {list.error && <ErrorState error={list.error} retry={() => list.mutate()} />}
      {list.isLoading && <div className="panel"><Loading rows={8} /></div>}
      {list.data && list.data.invoices.length === 0 && <div className="panel"><Empty title="No invoices here">Invoices appear once a document from the inbox has been read.</Empty></div>}
      {!!list.data?.invoices.length && (
        <div className="panel table-wrap">
          <table className="table">
            <thead><tr><th>Number</th><th>Vendor</th><th>Date</th><th className="num">Total</th><th>Status</th></tr></thead>
            <tbody>
              {list.data.invoices.map((i) => (
                <tr key={i.id} onClick={() => router.push(`/invoices/?id=${i.id}`)} className="cursor-pointer">
                  <td><Link href={`/invoices/?id=${i.id}`}>{i.invoice_number ?? "no number"}</Link>{i.kind === "credit_note" && <span className="chip ml-2">credit note</span>}</td>
                  <td>{i.vendor_id ? names.get(i.vendor_id) : <span className="missing">{i.vendor_name_printed ?? "unknown vendor"}</span>}</td>
                  <td className="muted">{i.invoice_date ? day(i.invoice_date) : ""}</td>
                  <td className="num">{amount(i.total, i.currency)}</td>
                  <td><StatusChip status={i.status} label={INVOICE_STATUS[i.status]} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Page>
  );
}

/** One invoice as a case: the list on the left, the invoice on its sheet in the middle, the decision on the right. */
function InvoiceCase({ id }: { id: string }) {
  const { user } = useMe();
  const toast = useToast();
  const [status, setStatus] = useState("");
  const { list, names } = useInvoiceList(status);
  const data = useApi<Invoice & { lines: InvoiceLine[]; provenance: Provenance[] }>(`/api/invoices/${id}`);
  const match = useApi<{ status: string; po_id: string | null; manual: boolean; reasons: { code: string; detail: string }[]; lines: Record<string, string | null>[] }>(`/api/invoices/${id}/match`, { shouldRetryOnError: false });
  const exceptions = useApi<{ exceptions: Exception[] }>("/api/exceptions");
  const vendors = useApi<{ vendors: Vendor[] }>("/api/vendors");
  const orders = useApi<{ orders: { id: string; po_number: string; vendor_id: string }[] }>("/api/orders");
  const [active, setActive] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [showDoc, setShowDoc] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [po, setPo] = useState("");
  const [reason, setReason] = useState("");
  const canEdit = user && user.role !== "viewer";
  const decider = user?.role === "approver" || user?.role === "admin";

  if (data.error) return <Page title="Invoice"><ErrorState error={data.error} /></Page>;
  if (!data.data) return <Page title="Invoice" flush><Loading rows={10} /></Page>;
  const inv = data.data;
  const editable = !!canEdit && EDITABLE.includes(inv.status); // the API refuses changes to an invoice waiting for approval or decided
  const prov = new Map(inv.provenance.map((p) => [p.field, p]));
  const exception = exceptions.data?.exceptions.find((e) => e.invoice_id === id && e.status !== "resolved");
  const boxes = inv.provenance.filter((p) => p.located && p.bbox && p.page).map((p) => ({ page: p.page!, bbox: p.bbox!, label: p.field, active: p.field === active }));
  const vendorName = vendors.data?.vendors.find((v) => v.id === inv.vendor_id)?.name ?? inv.vendor_name_printed ?? "Unknown vendor";

  const save = async (key: string, value: string) => {
    try {
      await api(`/api/invoices/${id}/fields`, { method: "PATCH", json: { [key]: value === "" ? null : value } });
      setEditing(null);
      data.mutate(); match.mutate();
      toast("Saved. The invoice is checked again.", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not save.", "bad"); }
  };
  const sendBack = async () => {
    try {
      await api(`/api/invoices/${id}/send-back`, { method: "POST", json: { reason } });
      setReason(""); data.mutate(); match.mutate();
      toast("Sent back. Correct it, then match it again.", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not send it back.", "bad"); }
  };
  const rematch = async (poId?: string) => {
    try {
      await api(`/api/invoices/${id}/match`, { method: "POST", json: poId ? { po_id: poId } : {} });
      data.mutate(); match.mutate(); exceptions.mutate();
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not match.", "bad"); }
  };

  return (
    <Page flush title={`${inv.kind === "credit_note" ? "Credit note" : "Invoice"} ${inv.invoice_number ?? ""}`}
      lede={[vendorName, amount(inv.total, inv.currency)].filter(Boolean).join(", ")}
      actions={<>
        <StatusChip status={inv.status} label={INVOICE_STATUS[inv.status]} />
        {exception && <Link className="btn btn--sm" href={`/exceptions/?id=${exception.id}`}>Open the exception</Link>}
        <Link className="btn btn--sm" href="/invoices/">All invoices</Link>
      </>}>
      <div className="workspace">
        <section className="pane pane--list" aria-label="Invoices">
          <div className="queue-head">
            <label className="field"><span className="label">Show</span>
              <select className="select input--sm" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Show">
                {FILTERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </div>
          <ul className="queue">
            {list.data?.invoices.map((i) => (
              <li key={i.id}>
                <Link className="queue__item" href={`/invoices/?id=${i.id}`} aria-current={i.id === id ? "true" : undefined}>
                  <span className="queue__top"><span className="queue__ref">{i.invoice_number ?? "no number"}</span><StatusChip status={i.status} label={INVOICE_STATUS[i.status]} /></span>
                  <span className="queue__sub"><span>{i.vendor_id ? names.get(i.vendor_id) : i.vendor_name_printed ?? "unknown vendor"}</span><span className="queue__amt">{amount(i.total, i.currency)}</span></span>
                </Link>
              </li>
            ))}
          </ul>
        </section>

        <section className="pane pane--doc" aria-label="The invoice as it was received" data-shown={showDoc ? "true" : "false"}>
          <button type="button" className="btn btn--sm doc-toggle" aria-expanded={showDoc} onClick={() => setShowDoc((v) => !v)}>{showDoc ? "Hide the document" : "Show the document"}</button>
          <div className="sheet-stick">
            {inv.document_id ? <PageViewer documentId={inv.document_id} boxes={boxes} page={page} onPage={setPage} /> : <div className="panel p-5 muted">Entered without a document.</div>}
          </div>
        </section>

        <section className="pane pane--decide" aria-label="Decision">
          {inv.issues.length > 0 && <div className="banner banner--open m-4" role="status"><strong>To fix before matching:</strong> {inv.issues.join(" ")}</div>}
          {canEdit && !editable && (
            <div className="banner m-4" role="status">
              {inv.status === "pending_approval" ? (
                decider ? (
                  <form className="grid gap-2 w-full" onSubmit={(e) => { e.preventDefault(); void sendBack(); }}>
                    <span>Waiting for approval. To change it, send it back for correction:</span>
                    <input className="input" required minLength={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What needs correcting" aria-label="Why it is sent back" />
                    <div><button className="btn btn--sm">Send back for correction</button></div>
                  </form>
                ) : "Waiting for approval, so it cannot be changed. An approver can send it back for correction."
              ) : "Decided, so it cannot be changed. An administrator can reopen an approved invoice."}
            </div>
          )}

          <section className="sec">
            <div className="sec__head"><h2>What was read</h2><span className="label">Select a value to see where it is on the page</span></div>
            <div className="fields">
              {FIELDS.map((f) => {
                const p = prov.get(f.from);
                const value = inv[f.key] as string | null;
                return (
                  <div key={f.key}>
                    <span className="field-label">{f.label}</span>
                    {editing === f.key ? (
                      <form onSubmit={(e) => { e.preventDefault(); save(f.key, draft); }} className="edit">
                        <input className="input" type={f.input ?? "text"} value={draft} onChange={(e) => setDraft(e.target.value)} aria-label={f.label} autoFocus />
                        <button className="btn btn--sm btn--primary">Save</button>
                      </form>
                    ) : (
                      <button className={`field-pick ${FIGURES.has(f.key as string) ? "measured" : ""}`} onClick={() => { setActive(f.from); if (p?.page) setPage(p.page); }} aria-label={`${f.label}: ${value ?? "missing"}`}>
                        {value != null && value !== "" ? (["subtotal", "tax", "total"].includes(f.key as string) ? amount(value) : value) : <span className="missing">missing{p?.candidate ? ` (rejected: "${p.candidate}", not on the page)` : ""}</span>}
                      </button>
                    )}
                    <span className="field-meta">{p?.located ? `${Math.round((p.confidence ?? 0) * 100)}%` : ""}
                      {editable && editing !== f.key && <button className="btn btn--sm btn--quiet" onClick={() => { setEditing(f.key); setDraft(value ?? ""); }}>Correct</button>}</span>
                  </div>
                );
              })}
              <div>
                <span className="field-label">Vendor</span>
                {editable ? (
                  <select className="select input--sm" value={inv.vendor_id ?? ""} onChange={(e) => save("vendor_id", e.target.value)} aria-label="Vendor">
                    <option value="">Not matched to a vendor</option>
                    {vendors.data?.vendors.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
                  </select>
                ) : <span>{vendors.data?.vendors.find((v) => v.id === inv.vendor_id)?.name ?? "not matched"}</span>}
                <span />
              </div>
            </div>
          </section>

          <section className="sec">
            <div className="sec__head"><h2>Lines</h2></div>
            {inv.lines.length === 0 ? <p className="muted">No lines were read.</p> : (
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Item</th><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th></tr></thead>
                  <tbody>{inv.lines.map((ln) => <tr key={ln.id}><td>{ln.item_code}</td><td>{ln.description}</td><td className="num">{ln.quantity}</td><td className="num">{ln.unit_price}</td><td className="num">{amount(ln.amount)}</td></tr>)}</tbody>
                </table>
              </div>
            )}
          </section>

          <section className="sec">
            <div className="sec__head">
              <h2>Match</h2>
              {canEdit && ["ready", "exception", "matched", "pending_approval", "on_hold"].includes(inv.status) && <button className="btn btn--sm" onClick={() => rematch()}>Match again</button>}
            </div>
            {match.data ? <MatchTable match={match.data} /> : <p className="muted">{inv.status === "needs_review" ? "Correct the fields above first; then it is matched." : "Not matched yet."}</p>}
            {canEdit && inv.status !== "approved" && inv.status !== "rejected" && (
              <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); if (po) rematch(po); }}>
                <select className="select" value={po} onChange={(e) => setPo(e.target.value)} aria-label="Purchase order">
                  <option value="">Match by hand to a purchase order…</option>
                  {orders.data?.orders.filter((o) => !inv.vendor_id || o.vendor_id === inv.vendor_id).map((o) => <option key={o.id} value={o.id}>{o.po_number}</option>)}
                </select>
                <button className="btn btn--sm" disabled={!po}>Match to this order</button>
              </form>
            )}
          </section>
        </section>
      </div>
    </Page>
  );
}

function InvoicesRoute() {
  const id = useSearchParams().get("id");
  return id ? <InvoiceCase key={id} id={id} /> : <InvoiceQueue />;
}

export default function Invoices() {
  return <Suspense fallback={<Loading />}><InvoicesRoute /></Suspense>;
}
