"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { amount, day, INVOICE_STATUS } from "@/lib/format";
import type { Vendor } from "@/lib/types";

interface OrderRow { id: string; po_number: string; vendor_id: string; currency: string; status: string; created_at: string }
interface OrderLine { id: string; line_no: number; item_code: string | null; description: string; quantity: string; unit_price: string; received: string }
interface Order extends Omit<OrderRow, "created_at"> {
  lines: OrderLine[];
  receipts: { id: string; receipt_no: string; received_at: string }[];
  changes: { at: string; changed_by: string | null; before: { lines: OrderLine[] }; after: { lines: OrderLine[] } }[];
}
interface InvoiceRow { id: string; invoice_number: string | null; po_number: string | null; vendor_id: string | null; total: string | null; currency: string | null; status: string; invoice_date: string | null }

/** The purchase orders, one row each, with the selected one open beside its sheet. */
function OrderCase({ id, vendorName, canEdit }: { id: string; vendorName: (vid: string) => string; canEdit: boolean }) {
  const toast = useToast();
  const order = useApi<Order>(`/api/orders/${id}`);
  const invoices = useApi<{ invoices: InvoiceRow[] }>("/api/invoices?limit=500");
  const [receiving, setReceiving] = useState(false);
  const [receiptNo, setReceiptNo] = useState("");
  const [qty, setQty] = useState<Record<string, string>>({});
  if (order.error) return <><section className="pane pane--doc"><ErrorState error={order.error} /></section></>;
  if (!order.data) return <><section className="pane pane--doc"><Loading rows={8} label="Loading the order" /></section></>;
  const po = order.data;
  const outstanding = po.lines.filter((l) => Number(l.received) < Number(l.quantity));
  const total = po.lines.reduce((s, l) => s + Number(l.quantity) * Number(l.unit_price), 0);
  const matched = (invoices.data?.invoices ?? []).filter((i) => i.po_number && i.po_number === po.po_number);

  const receive = async () => {
    const lines = Object.entries(qty).filter(([, q]) => Number(q) > 0).map(([po_line_id, quantity]) => ({ po_line_id, quantity }));
    if (!receiptNo || lines.length === 0) { toast("Give a receipt number and at least one quantity.", "bad"); return; }
    try {
      await api(`/api/orders/${id}/receipts`, { method: "POST", json: { receipt_no: receiptNo, lines } });
      setReceiving(false); setQty({}); setReceiptNo("");
      order.mutate();
      toast("Receipt recorded.", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.message : "Could not record the receipt.", "bad"); }
  };
  const setStatus = async (status: "open" | "closed") => {
    try { await api(`/api/orders/${id}`, { method: "PATCH", json: { status } }); order.mutate(); }
    catch (e) { toast(e instanceof ApiError ? e.message : "Could not change the order.", "bad"); }
  };

  return (
    <>
      <section className="pane pane--doc" aria-label="The purchase order">
        <article className="sheet paper" aria-label={`Purchase order ${po.po_number}`}>
          <header className="paper__head">
            <div className="grid gap-1 min-w-0">
              <h2>Purchase order {po.po_number}</h2>
              <span className="ink-2">{vendorName(po.vendor_id)}</span>
            </div>
            <StatusChip status={po.status === "open" ? "waiting" : "done"} label={po.status} />
          </header>
          <dl className="facts">
            <dt>Total</dt><dd>{amount(total.toFixed(2), po.currency)}</dd>
            <dt>Currency</dt><dd>{po.currency}</dd>
            <dt>Lines</dt><dd>{po.lines.length}{outstanding.length > 0 && `, ${outstanding.length} with goods to come`}</dd>
          </dl>
          <section className="paper__section" aria-label="Lines">
            <h3>Lines, as ordered, with what has arrived</h3>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Line</th><th>Item</th><th>Description</th><th className="num">Ordered</th><th className="num">Received</th><th className="num">Unit price</th></tr></thead>
                <tbody>{po.lines.map((l) => (
                  <tr key={l.id}><td>{l.line_no}</td><td>{l.item_code}</td><td>{l.description}</td><td className="num">{l.quantity}</td>
                    <td className={`num ${Number(l.received) < Number(l.quantity) ? "under" : ""}`}>{l.received}</td><td className="num">{l.unit_price}</td></tr>
                ))}</tbody>
              </table>
            </div>
          </section>
          <section className="paper__section" aria-label="Changes">
            <h3>Changes since it was raised</h3>
            {po.changes.length === 0 ? <p className="muted">Unchanged since it was raised.</p> : (
              <ul className="grid gap-2 m-0 pl-5">{po.changes.map((c, i) => {
                const diffs = c.after.lines.flatMap((a) => {
                  const b = c.before.lines.find((x) => x.id === a.id);
                  if (!b) return [];
                  return (["quantity", "unit_price", "description"] as const).filter((k) => a[k] !== b[k]).map((k) => `line ${a.line_no} ${k.replace("_", " ")} ${b[k]} to ${a[k]}`);
                });
                return <li key={i}><span className="muted">{day(c.at)}</span> {diffs.length ? diffs.join("; ") : "status changed"}</li>;
              })}</ul>
            )}
          </section>
          {canEdit && (
            <div className="flex flex-wrap gap-2">
              {po.status === "open" && outstanding.length > 0 && <button className="btn btn--sm btn--primary" onClick={() => setReceiving((r) => !r)} aria-expanded={receiving}>Record a receipt</button>}
              <button className="btn btn--sm" onClick={() => setStatus(po.status === "open" ? "closed" : "open")}>{po.status === "open" ? "Close the order" : "Reopen"}</button>
            </div>
          )}
        </article>
      </section>

      <section className="pane pane--decide" aria-label="Receipts and invoices">
        <div className="decide">
          {receiving && (
            <form className="grid gap-4" aria-label="Goods received" onSubmit={(e) => { e.preventDefault(); receive(); }}>
              <div className="decide__head"><h2>Goods received</h2><span className="ink-2">Enter what arrived on each line.</span></div>
              <label className="field"><span className="label">Receipt number</span><input className="input" value={receiptNo} onChange={(e) => setReceiptNo(e.target.value)} required /></label>
              <ul className="grid gap-3 m-0 p-0" style={{ listStyle: "none" }}>
                {outstanding.map((l) => (
                  <li key={l.id} className="grid gap-1">
                    <span className="flex flex-wrap justify-between gap-2"><span>Line {l.line_no}: {l.description}</span><span className="muted measured">{(Number(l.quantity) - Number(l.received)).toString()} to come</span></span>
                    <input className="input measured" inputMode="decimal" aria-label={`Received on line ${l.line_no}`} value={qty[l.id] ?? ""} onChange={(e) => setQty({ ...qty, [l.id]: e.target.value })} />
                  </li>
                ))}
              </ul>
              <div className="decide__row"><button className="btn btn--primary">Record</button><button type="button" className="btn btn--quiet" onClick={() => setReceiving(false)}>Cancel</button></div>
            </form>
          )}

          <section className="grid gap-2" aria-label="Receipts">
            <h2>Receipts</h2>
            {po.receipts.length === 0 ? <p className="muted">Nothing received yet.</p> : (
              <ul className="grid gap-2 m-0 p-0" style={{ listStyle: "none" }}>
                {po.receipts.map((r) => <li key={r.id} className="flex flex-wrap justify-between gap-2"><strong className="measured">{r.receipt_no}</strong><span className="muted">{day(r.received_at)}</span></li>)}
              </ul>
            )}
          </section>

          <section className="grid gap-2" aria-label="Invoices for this order">
            <h2>Invoices for this order</h2>
            <span className="label">Found by the PO number printed on them.</span>
            {invoices.isLoading && <Loading rows={2} />}
            {!invoices.isLoading && matched.length === 0 && <p className="muted">No invoice quotes this PO number yet.</p>}
            <ul className="queue well">
              {matched.map((i) => (
                <li key={i.id}>
                  <Link className="queue__item" href={`/invoices/?id=${i.id}`}>
                    <span className="queue__top"><span className="queue__ref">{i.invoice_number ?? "no number"}</span><span className="queue__amt">{amount(i.total, i.currency)}</span></span>
                    <span className="queue__sub"><span>{i.invoice_date ? day(i.invoice_date) : ""}</span><StatusChip status={i.status} label={INVOICE_STATUS[i.status]} /></span>
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </section>
    </>
  );
}

function OrdersRoute() {
  const id = useSearchParams().get("id");
  const { user } = useMe();
  const list = useApi<{ orders: OrderRow[] }>("/api/orders");
  const vendors = useApi<{ vendors: Vendor[] }>("/api/vendors");
  const names = new Map((vendors.data?.vendors ?? []).map((v) => [v.id, v.name]));
  const vendorName = (vid: string) => names.get(vid) ?? "";
  const canEdit = !!user && user.role !== "viewer";
  const selected = list.data?.orders.find((o) => o.id === id);
  return (
    <Page flush title="Purchase orders"
      lede={id ? [vendorName(selected?.vendor_id ?? ""), selected?.currency].filter(Boolean).join(", ") : "What was ordered and what has arrived. Invoices are matched against both."}
      actions={id ? <Link className="btn btn--sm" href="/orders/">All orders</Link> : undefined}>
      <div className="workspace" data-picked={id ? "true" : "false"}>
        <section className="pane pane--list" aria-label="Purchase orders">
          {list.error && <div className="bar-note"><ErrorState error={list.error} retry={() => list.mutate()} /></div>}
          {list.isLoading && <Loading rows={8} label="Loading the orders" />}
          {list.data && list.data.orders.length === 0 && <Empty title="No purchase orders yet" />}
          {!!list.data?.orders.length && (
            <ul className="queue">
              {list.data.orders.map((o) => (
                <li key={o.id}>
                  <Link className="queue__item" href={`/orders/?id=${o.id}`} aria-current={o.id === id ? "true" : undefined}>
                    <span className="queue__top"><span className="queue__ref">{o.po_number}</span><StatusChip status={o.status === "open" ? "waiting" : "done"} label={o.status} /></span>
                    <span className="queue__sub"><span>{vendorName(o.vendor_id)}</span><span className="queue__age">{day(o.created_at)}</span></span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
        {id ? (
          <OrderCase key={id} id={id} vendorName={vendorName} canEdit={canEdit} />
        ) : (
          <section className="pane pane--idle" aria-label="Choose an order">
            <strong>Choose a purchase order.</strong>
            <span>The order sits on the sheet, with its receipts and invoices beside it.</span>
          </section>
        )}
      </div>
    </Page>
  );
}

export default function Orders() {
  return <Suspense fallback={<Loading />}><OrdersRoute /></Suspense>;
}
