"""Deterministic calculations over stored query results.

The model never does arithmetic. It names an operation and the columns of a result it has already fetched
(by evidence handle), and these functions produce the figures, using Decimal throughout. Inputs are always
references to stored rows, never numbers typed by the model, so a calculated figure is traceable to a query.

Missing data is reported, not hidden: an empty input raises ComputeError('no_data'); a NULL or a division by
zero yields None for that figure together with a note.
"""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

Table = dict[str, Any]  # {"columns": [...], "rows": [[...], ...]}
Load = Callable[[str], Table | None]


class ComputeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class ComputeResult:
    op: str
    values: dict[str, float | None] = field(default_factory=dict)
    table: list[dict] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"op": self.op, "values": self.values, "table": self.table, "notes": self.notes}


# ---------------------------------------------------------------- helpers


def _dec(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _out(value: Decimal | None, places: int = 6) -> float | None:
    return None if value is None else float(round(value, places))


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _need(args: dict, *names: str) -> list:
    missing = [n for n in names if args.get(n) in (None, "", [])]
    if missing:
        raise ComputeError("bad_args", f"Missing argument(s): {', '.join(missing)}.")
    return [args[n] for n in names]


def _rows(args: dict, load: Load, *columns: str) -> list[dict]:
    """The referenced result as a list of dicts, after checking that the named columns exist."""
    (handle,) = _need(args, "handle")
    table = load(handle)
    if table is None:
        raise ComputeError("bad_args", f"Unknown evidence handle '{handle}'.")
    have = table["columns"]
    for column in columns:
        if column not in have:
            raise ComputeError("bad_column", f"Column '{column}' is not in {handle}. Columns: {', '.join(have)}.")
    rows = [dict(zip(have, row, strict=True)) for row in table["rows"]]
    if not rows:
        raise ComputeError("no_data", f"{handle} has no rows, so there is nothing to calculate.")
    return rows


def _in_order(rows: list[dict], column: str) -> list[dict]:
    """Sort by the column's own values (numbers as numbers, dates as dates); mixed types fall back to text."""
    try:
        return sorted(rows, key=lambda r: r[column])
    except TypeError:
        return sorted(rows, key=lambda r: str(r[column]))


def _as_list(value) -> list[str]:
    return [value] if isinstance(value, str) else list(value or [])


def _grouped(rows: list[dict], keys: list[str]) -> "OrderedDict[tuple, list[dict]]":
    groups: OrderedDict[tuple, list[dict]] = OrderedDict()
    for row in rows:
        groups.setdefault(tuple(row[k] for k in keys), []).append(row)
    return groups


def _label(keys: list[str], key: tuple) -> dict:
    return dict(zip(keys, key, strict=True))


# ---------------------------------------------------------------- operations


def growth(args: dict, load: Load) -> ComputeResult:
    """Change between consecutive periods. Args: handle, value, order_by, [group_by]."""
    value, order_by = _need(args, "value", "order_by")
    group_by = _as_list(args.get("group_by"))
    rows = _rows(args, load, value, order_by, *group_by)
    result = ComputeResult("growth", table=[])
    for key, members in _grouped(rows, group_by).items():
        members = _in_order(members, order_by)
        previous: Decimal | None = None
        for i, row in enumerate(members):
            current = _dec(row[value])
            change = current - previous if current is not None and previous is not None else None
            pct = _div(change, abs(previous)) if previous is not None else None
            if current is None:
                result.notes.append(f"{value} is missing for {row[order_by]}; change not calculated.")
            elif i > 0 and previous == 0:
                result.notes.append(f"Previous {value} is zero for {row[order_by]}; percentage change undefined.")
            result.table.append(
                {**_label(group_by, key), order_by: row[order_by], value: _out(current), "previous": _out(previous),
                 "change": _out(change), "change_pct": _out(pct)}
            )  # fmt: skip
            previous = current
    if not group_by:
        first, last = _dec(result.table[0][value]), _dec(result.table[-1][value])
        total = last - first if first is not None and last is not None else None
        result.values = {
            "first": _out(first), "last": _out(last), "total_change": _out(total),
            "total_change_pct": _out(_div(total, abs(first)) if first is not None else None),
        }  # fmt: skip
    return result


def share(args: dict, load: Load) -> ComputeResult:
    """Each row's share of the total. Args: handle, value, label."""
    value, label = _need(args, "value", "label")
    rows = _rows(args, load, value, label)
    usable = [(r[label], _dec(r[value])) for r in rows]
    result = ComputeResult("share")
    if skipped := [str(name) for name, v in usable if v is None]:
        result.notes.append(f"{value} is missing for: {', '.join(skipped)}; excluded from the total.")
    total = sum((v for _, v in usable if v is not None), Decimal(0))
    if total == 0:
        result.notes.append("The total is zero; shares are undefined.")
    result.values = {"total": _out(total)}
    result.table = [{label: name, value: _out(v), "share": _out(_div(v, total))} for name, v in usable if v is not None]
    return result


def margin(args: dict, load: Load) -> ComputeResult:
    """(revenue - cost) / revenue, or profit / revenue. Args: handle, revenue, cost or profit, [group_by]."""
    (revenue,) = _need(args, "revenue")
    cost, profit = args.get("cost"), args.get("profit")
    if bool(cost) == bool(profit):
        raise ComputeError("bad_args", "Give exactly one of 'cost' or 'profit'.")
    group_by = _as_list(args.get("group_by"))
    rows = _rows(args, load, revenue, cost or profit, *group_by)
    result = ComputeResult("margin", table=[])
    total_rev = total_profit = Decimal(0)
    incomplete = False
    for key, members in _grouped(rows, group_by).items():
        if any(_dec(r[revenue]) is None or _dec(r[cost or profit]) is None for r in members):
            # A missing cost is not a cost of zero: the margin is unknown, and so is the overall figure.
            result.notes.append(
                f"A value is missing for {_label(group_by, key) or 'the selection'}; margin not calculated."
            )
            result.table.append({**_label(group_by, key), "revenue": None, "profit": None, "margin": None})
            incomplete = True
            continue
        rev = sum((_dec(r[revenue]) for r in members), Decimal(0))
        other = sum((_dec(r[cost or profit]) for r in members), Decimal(0))
        prof = other if profit else rev - other
        if rev == 0:
            result.notes.append(f"Revenue is zero for {_label(group_by, key) or 'the selection'}; margin undefined.")
        total_rev, total_profit = total_rev + rev, total_profit + prof
        result.table.append(
            {**_label(group_by, key), "revenue": _out(rev, 2), "profit": _out(prof, 2), "margin": _out(_div(prof, rev))}
        )
    if incomplete:
        result.values = {"revenue": None, "profit": None, "margin": None}
    else:
        result.values = {
            "revenue": _out(total_rev, 2), "profit": _out(total_profit, 2), "margin": _out(_div(total_profit, total_rev)),
        }  # fmt: skip
    return result


def variance_decomposition(args: dict, load: Load) -> ComputeResult:
    """Split a revenue change between two periods into price, volume and mix effects.

    Args: handle, item (e.g. product or segment column), period, quantity, value, base_period, compare_period.
    With P0 the base average price (R0 / Q0), and p, q the price and quantity of each item:
        volume = (Q1 - Q0) * P0
        mix    = sum((q1/Q1 - q0/Q0) * Q1 * p0)
        price  = sum((p1 - p0) * q1)
    which add up exactly to R1 - R0. An item sold in only one period takes its price from that period.
    """
    item, period, quantity, value, base, compare = _need(
        args, "item", "period", "quantity", "value", "base_period", "compare_period"
    )
    rows = _rows(args, load, item, period, quantity, value)
    cells: dict[Any, dict[str, tuple[Decimal, Decimal]]] = {}
    for row in rows:
        which = "base" if str(row[period]) == str(base) else "compare" if str(row[period]) == str(compare) else None
        q, v = _dec(row[quantity]), _dec(row[value])
        if which is None or q is None or v is None:
            continue
        q0, v0 = cells.setdefault(row[item], {}).get(which, (Decimal(0), Decimal(0)))
        cells[row[item]][which] = (q0 + q, v0 + v)
    zero = (Decimal(0), Decimal(0))
    q0_total = sum((c.get("base", zero)[0] for c in cells.values()), Decimal(0))
    q1_total = sum((c.get("compare", zero)[0] for c in cells.values()), Decimal(0))
    r0 = sum((c.get("base", zero)[1] for c in cells.values()), Decimal(0))
    r1 = sum((c.get("compare", zero)[1] for c in cells.values()), Decimal(0))
    if q0_total == 0 or q1_total == 0:
        which = base if q0_total == 0 else compare
        raise ComputeError("no_data", f"No quantity found for period '{which}'; check the period values.")
    p0_avg = r0 / q0_total
    volume = (q1_total - q0_total) * p0_avg
    mix = price = Decimal(0)
    table = []
    for name, c in cells.items():
        (q0, v0), (q1, v1) = c.get("base", zero), c.get("compare", zero)
        p1 = v1 / q1 if q1 else None
        p0 = v0 / q0 if q0 else p1
        p1 = p1 if p1 is not None else p0
        item_mix = (q1 / q1_total - q0 / q0_total) * q1_total * p0
        item_price = (p1 - p0) * q1
        mix, price = mix + item_mix, price + item_price
        table.append({item: name, "price_effect": _out(item_price, 2), "mix_effect": _out(item_mix, 2),
                      "base_value": _out(v0, 2), "compare_value": _out(v1, 2)})  # fmt: skip
    table.sort(key=lambda r: abs(r["price_effect"]) + abs(r["mix_effect"]), reverse=True)
    return ComputeResult(
        "variance_decomposition",
        values={"base_value": _out(r0, 2), "compare_value": _out(r1, 2), "total_change": _out(r1 - r0, 2),
                "price_effect": _out(price, 2), "volume_effect": _out(volume, 2), "mix_effect": _out(mix, 2)},
        table=table[:25],
    )  # fmt: skip


def zscore_anomalies(args: dict, load: Load) -> ComputeResult:
    """Rows whose value is unusually far from the mean. Args: handle, value, [label], [threshold=2.0]."""
    (value,) = _need(args, "value")
    label = args.get("label")
    threshold = _dec(args.get("threshold", 2.0))
    if threshold is None or threshold <= 0:
        raise ComputeError("bad_args", "threshold must be a positive number.")
    rows = _rows(args, load, value, *([label] if label else []))
    points = [(r.get(label) if label else i, _dec(r[value])) for i, r in enumerate(rows)]
    points = [(name, v) for name, v in points if v is not None]
    result = ComputeResult("zscore_anomalies", table=[])
    if len(points) < 3:
        raise ComputeError("no_data", "At least three values are needed to look for anomalies.")
    mean = sum((v for _, v in points), Decimal(0)) / len(points)
    variance = sum(((v - mean) ** 2 for _, v in points), Decimal(0)) / (len(points) - 1)
    std = variance.sqrt()
    result.values = {"mean": _out(mean), "std": _out(std), "count": float(len(points))}
    if std == 0:
        result.notes.append("All values are identical; there are no anomalies.")
        return result
    for name, v in points:
        z = (v - mean) / std
        if abs(z) >= threshold:
            result.table.append({(label or "row"): name, value: _out(v), "z": _out(z, 3)})
    result.table.sort(key=lambda r: abs(r["z"]), reverse=True)
    return result


def seasonal_forecast(args: dict, load: Load) -> ComputeResult:
    """A simple forecast: the same period one season ago, scaled by recent year-on-year growth.

    Args: handle, value, order_by, [periods=3], [season_length=12]. Needs at least two full seasons;
    with less history it falls back to the average change per period, and says so.
    """
    value, order_by = _need(args, "value", "order_by")
    periods, season = int(args.get("periods", 3)), int(args.get("season_length", 12))
    if not (1 <= periods <= 24 and 1 <= season <= 53):
        raise ComputeError("bad_args", "periods must be 1-24 and season_length 1-53.")
    rows = _in_order(_rows(args, load, value, order_by), order_by)
    series = [_dec(r[value]) for r in rows]
    if any(v is None for v in series):
        raise ComputeError("no_data", f"{value} has missing values; a forecast needs a complete series.")
    result = ComputeResult("seasonal_forecast", table=[])
    if len(series) >= 2 * season:
        recent, year_before = sum(series[-season:], Decimal(0)), sum(series[-2 * season : -season], Decimal(0))
        factor = _div(recent, year_before)
        if factor is None or factor == 0:
            # One of the two seasons sums to zero, so there is no growth rate to carry forward.
            result.values = {"yoy_growth": None}
            result.notes.append(
                "Year-on-year growth is undefined (a season sums to zero); repeating the last season unchanged."
            )
            factor = Decimal(1)
        else:
            result.values = {"yoy_growth": _out(factor - 1)}
            result.notes.append("Method: same period last season multiplied by the latest year-on-year growth.")
        extended = list(series)
        for step in range(1, periods + 1):
            extended.append(extended[-season] * factor)
            result.table.append({"step": step, "forecast": _out(extended[-1], 2)})
    elif len(series) >= 3:
        slope = (series[-1] - series[0]) / (len(series) - 1)
        result.values = {"average_change_per_period": _out(slope, 2)}
        result.notes.append(f"Fewer than {2 * season} periods of history; used the average change per period instead.")
        for step in range(1, periods + 1):
            result.table.append({"step": step, "forecast": _out(series[-1] + slope * step, 2)})
    else:
        raise ComputeError("no_data", "At least three periods are needed for a forecast.")
    return result


def handles_of(args: dict) -> list[str]:
    """The results a calculation reads: 'handle', 'handles', or both."""
    named = _as_list(args.get("handles")) + ([args["handle"]] if isinstance(args.get("handle"), str) else [])
    return list(dict.fromkeys(h for h in named if isinstance(h, str)))


def total(args: dict, load: Load) -> ComputeResult:
    """Sum of each named column over every row of one or more results, and the sum of those sums.

    Args: handle or handles, columns. Adds input and output tokens, or the cost of several projects fetched
    separately."""
    (columns,) = _need(args, "columns")
    columns = _as_list(columns)
    handles = handles_of(args)
    if not handles:
        raise ComputeError("bad_args", "Missing argument(s): handle or handles.")
    result = ComputeResult("total", table=[])
    sums = {c: Decimal(0) for c in columns}
    for handle in handles:
        for column in columns:
            rows = _rows({"handle": handle}, load, column)
            values = [_dec(r[column]) for r in rows]
            if missing := sum(v is None for v in values):
                result.notes.append(f"{column} is missing in {missing} row(s) of {handle}; those rows are left out.")
            part = sum((v for v in values if v is not None), Decimal(0))
            sums[column] += part
            result.table.append({"source": handle, "column": column, "rows": len(rows), "sum": _out(part)})
    result.values = {c: _out(s) for c, s in sums.items()} | {"total": _out(sum(sums.values(), Decimal(0)))}
    return result


OPS: dict[str, Callable[[dict, Load], ComputeResult]] = {
    "total": total,
    "growth": growth,
    "share": share,
    "margin": margin,
    "variance_decomposition": variance_decomposition,
    "zscore_anomalies": zscore_anomalies,
    "seasonal_forecast": seasonal_forecast,
}


def run_compute(op: str, args: dict, load: Load) -> ComputeResult:
    if op not in OPS:
        raise ComputeError("bad_args", f"Unknown operation '{op}'. Available: {', '.join(OPS)}.")
    if not isinstance(args, dict):
        raise ComputeError("bad_args", "args must be an object.")
    try:
        return OPS[op](args, load)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ComputeError("bad_args", f"Could not run {op}: {exc}") from exc
