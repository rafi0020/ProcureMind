import { StatusChip } from "./ui";

const STATUS: Record<string, string> = { matched: "matched", matched_with_variance: "matched with a variance", exception: "exception" };

export function MatchTable({ match }: { match: { status: string; manual: boolean; reasons: { code: string; detail: string }[]; lines: Record<string, string | null>[] } }) {
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2 items-center"><StatusChip status={match.status === "exception" ? "exception" : "matched"} label={STATUS[match.status]} />{match.manual && <span className="chip">matched by hand</span>}</div>
      {match.lines.length === 0 && match.status !== "exception" && <p className="ink-2">No lines were read, so the subtotal was checked against the value received less what other invoices already billed on this order.</p>}
      {match.reasons.length > 0 && <ul className="grid gap-1">{match.reasons.map((r, i) => <li key={i}><strong>{r.code.replace(/_/g, " ")}</strong>: <span className="ink-2">{r.detail}</span></li>)}</ul>}
      {match.lines.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Line</th><th>Item</th><th className="num">Ordered</th><th className="num">Received</th><th className="num">Billed before</th><th className="num">This invoice</th><th className="num">PO price</th><th className="num">Invoice price</th><th className="num">Variance</th></tr></thead>
            <tbody>
              {match.lines.map((ln, i) => {
                const variance = ln.price_variance_pct == null ? null : Number(ln.price_variance_pct);
                const over = Number(ln.previously_invoiced ?? 0) + Number(ln.invoiced ?? 0) > Number(ln.received ?? ln.ordered ?? 0);
                return (
                  <tr key={i}>
                    <td>{ln.invoice_line_no ?? ln.line_no}</td><td className="whitespace-nowrap">{ln.item_code}</td><td className="num">{ln.ordered}</td><td className="num">{ln.received ?? ""}</td>
                    <td className="num">{ln.previously_invoiced ?? ""}</td><td className={`num ${over && ln.received != null ? "over" : ""}`}>{ln.invoiced}</td>
                    <td className="num">{ln.po_unit_price}</td><td className="num">{ln.unit_price}</td>
                    <td className={`num ${variance && variance > 0 ? "over" : "under"}`}>{variance == null ? "" : `${variance > 0 ? "+" : ""}${variance.toFixed(2)}%`}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
