"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Page } from "@/components/Shell";
import { ErrorState, Empty, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { amount, day, INVOICE_STATUS, when } from "@/lib/format";
import type { Invoice, InvoiceLine, Provenance } from "@/lib/types";

interface Waiting {
  id: string; invoice_number: string | null; vendor: string | null; total: string; currency: string; status: string;
  approvals: number; required: number; needs_admin: boolean; waiting_for: string | null; pending_since: string | null; escalated: boolean;
  match: { status: string; value: string } | null;
  exception: { resolution: string; resolved_by: string | null } | null;
}
interface MatchView { status: string; manual: boolean; reasons: { code: string; detail: string }[] }

const MATCH_WORDS: Record<string, string> = { matched: "matched", matched_with_variance: "matched with a variance", exception: "exception" };
const words = (code: string) => code.replace(/_/g, " ");

/** How long an invoice has been waiting, in hours then days. */
function waiting(since: string | null) {
  if (!since) return "";
  const hours = (Date.now() - Date.parse(since)) / 3_600_000;
  return hours < 48 ? `waiting ${Math.max(1, Math.floor(hours))} h` : `waiting ${Math.floor(hours / 24)} d`;
}

/** The selected invoice: its summary on a raised sheet in the middle, the decision beside it. */
function ApprovalCase({ id, item, decidable, decider, onDone }: { id: string; item: Waiting | undefined; decidable: boolean; decider: boolean; onDone: () => void }) {
  const toast = useToast();
  const detail = useApi<Invoice & { lines: InvoiceLine[]; provenance: Provenance[] }>(`/api/invoices/${id}`);
  const match = useApi<MatchView>(`/api/invoices/${id}/match`, { shouldRetryOnError: false });
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  if (detail.error) return <section className="pane pane--doc" aria-label="The invoice"><ErrorState error={detail.error} retry={() => detail.mutate()} /></section>;
  if (!detail.data) return (
    <>
      <section className="pane pane--doc" aria-label="The invoice"><Loading rows={10} label="Loading the invoice" /></section>
      <section className="pane pane--decide" aria-label="Decision"><Loading rows={4} label="Loading the decision" /></section>
    </>
  );
  const inv = detail.data;
  const vendor = item?.vendor ?? inv.vendor_name_printed ?? "Unknown vendor";
  const decide = async (decision: "approved" | "rejected") => {
    setBusy(true);
    try {
      const r = await api<{ status: string; waiting_for: string | null }>(`/api/ap-approvals/${id}/decide`, { method: "POST", json: { decision, reason: reason || null } });
      toast(r.status === "approved" ? "Approved for payment." : r.status === "rejected" ? "Rejected." : `Recorded. Still waiting for ${r.waiting_for}.`, "ok");
      setRejecting(false); setReason("");
      onDone();
      void detail.mutate();
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not record the decision.", "bad"); }
    finally { setBusy(false); }
  };

  return (
    <>
      <section className="pane pane--doc" aria-label="The invoice">
        <article className="sheet paper" aria-label={`Invoice ${inv.invoice_number ?? ""}`}>
          <header className="paper__head">
            <div className="grid gap-1 min-w-0">
              <h2>Invoice {inv.invoice_number ?? "no number"}</h2>
              <span className="ink-2">{vendor}</span>
            </div>
            <StatusChip status={inv.status} label={INVOICE_STATUS[inv.status]} />
          </header>
          <dl className="facts">
            <dt>Invoice date</dt><dd>{inv.invoice_date ? day(inv.invoice_date) : "not read"}</dd>
            <dt>Due date</dt><dd>{inv.due_date ? day(inv.due_date) : "not read"}</dd>
            <dt>PO number</dt><dd>{inv.po_number ?? "none printed"}</dd>
            <dt>Subtotal</dt><dd>{inv.subtotal ? amount(inv.subtotal) : "not read"}</dd>
            <dt>Tax</dt><dd>{inv.tax ? amount(inv.tax) : "not read"}</dd>
            <dt>Total</dt><dd>{inv.total ? amount(inv.total, inv.currency) : "not read"}</dd>
            <dt>Bank account</dt><dd>{inv.bank_account ?? "not read"}</dd>
          </dl>
          <section className="paper__section" aria-label="Lines">
            <h3>Lines</h3>
            {inv.lines.length === 0 ? <p className="muted">No lines were read.</p> : (
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Item</th><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th></tr></thead>
                  <tbody>{inv.lines.map((ln) => <tr key={ln.id}><td>{ln.item_code}</td><td>{ln.description}</td><td className="num">{ln.quantity}</td><td className="num">{ln.unit_price}</td><td className="num">{amount(ln.amount)}</td></tr>)}</tbody>
                </table>
              </div>
            )}
          </section>
          <section className="paper__section" aria-label="Match">
            <h3>Match</h3>
            {match.data ? (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <StatusChip status={match.data.status === "exception" ? "exception" : "matched"} label={MATCH_WORDS[match.data.status] ?? match.data.status} />
                  {match.data.manual && <span className="chip">matched by hand</span>}
                  {item?.exception?.resolution === "approve_with_variance" && <span className="chip chip--matched">variance accepted by {item.exception.resolved_by ?? "a colleague"}</span>}
                </div>
                {match.data.reasons.length > 0 && <ul className="grid gap-1 m-0 pl-5">{match.data.reasons.map((r, i) => <li key={i}><strong>{words(r.code)}</strong>: <span className="ink-2">{r.detail}</span></li>)}</ul>}
              </>
            ) : <p className="muted">{inv.status === "needs_review" ? "Correct the fields on the invoice first; then it is matched." : "Not matched yet."}</p>}
            <div><Link className="btn btn--sm btn--quiet" href={`/invoices/?id=${id}`}>Open the invoice and its document</Link></div>
          </section>
        </article>
      </section>

      <section className="pane pane--decide" aria-label="Decision">
        <div className="decide">
          <div className="decide__head">
            <span className="label">Total to pay</span>
            <span className="decide__amount">{inv.total ? amount(inv.total, inv.currency) : "not read"}</span>
            <span className="ink-2">{vendor}</span>
          </div>

          {item ? (
            <>
              <div className="decide__facts">
                <span className="ink-2">{item.approvals} of {item.required} approvals so far{item.needs_admin && ", and one must be an administrator"}.</span>
                <span className="muted">{waiting(item.pending_since)}{item.escalated && <> · <span className="chip chip--bad ml-1">overdue</span></>}</span>
              </div>
              {decidable ? (
                rejecting ? (
                  <form className="decide__note" onSubmit={(e) => { e.preventDefault(); void decide("rejected"); }}>
                    <label className="field"><span className="label">Reason for rejecting</span>
                      <input className="input" required minLength={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Say why, so the clerk can correct it" aria-label={`Reason for rejecting ${inv.invoice_number ?? ""}`} autoFocus />
                    </label>
                    <div className="decide__row">
                      <button className="btn btn--danger" disabled={busy}>Reject</button>
                      <button type="button" className="btn btn--quiet" onClick={() => setRejecting(false)}>Cancel</button>
                    </div>
                  </form>
                ) : (
                  <div className="decide__row">
                    <button className="btn btn--primary" disabled={busy} onClick={() => decide("approved")}>Approve</button>
                    <button className="btn" disabled={busy} onClick={() => setRejecting(true)}>Reject…</button>
                  </div>
                )
              ) : (
                <div className="grid gap-2">
                  <StatusChip status="waiting" label="someone else decides" />
                  {decider
                    ? <p className="decide__says">You entered or corrected this invoice, so another person decides it. The rule keeps one person from both making and approving a change.</p>
                    : <p className="decide__says">Approvers and administrators decide. You can see everything waiting.</p>}
                </div>
              )}
            </>
          ) : (
            <div className="banner" role="status">This invoice is no longer waiting for a decision. Its status is in the header of the sheet.</div>
          )}
          <div className="decide__facts">
            <span className="label">Waiting since</span>
            <span>{item?.pending_since ? when(item.pending_since) : "not waiting"}</span>
          </div>
        </div>
      </section>
    </>
  );
}

function ApprovalsRoute() {
  const id = useSearchParams().get("id");
  const { user } = useMe();
  const [tab, setTab] = useState<"mine" | "all">("mine");
  const all = useApi<{ invoices: Waiting[] }>("/api/ap-approvals?mine=false");
  const mine = useApi<{ invoices: Waiting[] }>("/api/ap-approvals?mine=true");
  const decider = user?.role === "approver" || user?.role === "admin";
  const visible = (tab === "mine" ? mine.data : all.data)?.invoices ?? [];
  const decidable = new Set((mine.data?.invoices ?? []).map((i) => i.id));
  const item = all.data?.invoices.find((i) => i.id === id);
  const refresh = () => { void all.mutate(); void mine.mutate(); };
  const loading = tab === "mine" ? mine.isLoading : all.isLoading;
  const error = all.error ?? mine.error;

  return (
    <Page flush title="Approvals" lede="Matched invoices waiting for approval before payment. Overdue ones come first. You never approve an invoice you entered or corrected."
      actions={id ? <Link className="btn btn--sm" href="/approvals/">All approvals</Link> : undefined}>
      <div className="tabs tabs--bar" role="tablist" aria-label="Which invoices">
        <button role="tab" className="tab" aria-selected={tab === "mine"} onClick={() => setTab("mine")}>Mine to decide</button>
        <button role="tab" className="tab" aria-selected={tab === "all"} onClick={() => setTab("all")}>Everything waiting</button>
      </div>
      {!decider && tab === "mine" && <div className="bar-note"><div className="banner" role="status">Approvers and administrators decide. You can see everything waiting.</div></div>}
      {error && <div className="bar-note"><ErrorState error={error} retry={refresh} /></div>}

      <div className="workspace" data-picked={id ? "true" : "false"}>
        <section className="pane pane--list" aria-label="Approval queue">
          {loading && <Loading rows={5} label="Loading the queue" />}
          {!loading && !error && visible.length === 0 && <Empty title="Nothing waiting">{tab === "mine" ? "No invoices are waiting for you." : "No invoices are waiting for approval."}</Empty>}
          {!!visible.length && (
            <ul className="queue">
              {visible.map((i) => (
                <li key={i.id}>
                  <Link className="queue__item" href={`/approvals/?id=${i.id}`} aria-current={i.id === id ? "true" : undefined}>
                    <span className="queue__top"><span className="queue__ref">{i.invoice_number ?? "no number"}</span><span className="queue__amt">{amount(i.total, i.currency)}</span></span>
                    <span className="queue__sub"><span>{i.vendor ?? "unknown vendor"}</span><span className="queue__age">{i.escalated ? <span className="chip chip--bad">overdue</span> : waiting(i.pending_since)}</span></span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        {id ? (
          <ApprovalCase key={id} id={id} item={item} decidable={decidable.has(id)} decider={decider} onDone={refresh} />
        ) : (
          <section className="pane pane--idle" aria-label="Choose an invoice">
            <strong>Choose an invoice from the queue.</strong>
            <span>Its header, lines and match appear on the sheet; the decision is beside it.</span>
          </section>
        )}
      </div>
    </Page>
  );
}

export default function Approvals() {
  return <Suspense fallback={<Loading />}><ApprovalsRoute /></Suspense>;
}
