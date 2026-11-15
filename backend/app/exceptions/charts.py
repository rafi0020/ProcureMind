"""Chart specifications built from stored results. The browser draws them; no numbers are invented here."""

from typing import Literal

from app.exceptions.compute import ComputeError, Load

MAX_POINTS = 200
ChartKind = Literal["line", "bar", "waterfall"]


def build_chart(kind: str, handle: str, x: str, y: list[str], title: str, load: Load) -> dict:
    if kind not in ("line", "bar", "waterfall"):
        raise ComputeError("bad_args", "kind must be line, bar or waterfall.")
    table = load(handle)
    if table is None:
        raise ComputeError("bad_args", f"Unknown evidence handle '{handle}'.")
    columns = table["columns"]
    for column in [x, *y]:
        if column not in columns:
            raise ComputeError("bad_column", f"Column '{column}' is not in {handle}. Columns: {', '.join(columns)}.")
    if not y or (kind == "waterfall" and len(y) != 1):
        raise ComputeError("bad_args", "Give one y column for a waterfall, one or more otherwise.")
    if not table["rows"]:
        raise ComputeError("no_data", f"{handle} has no rows to chart.")
    rows = table["rows"][:MAX_POINTS]
    xi = columns.index(x)
    return {
        "kind": kind,
        "title": title[:120],
        "handle": handle,
        "x": x,
        "labels": [str(r[xi]) for r in rows],
        "series": [{"name": name, "data": [r[columns.index(name)] for r in rows]} for name in y],
        "truncated": len(table["rows"]) > MAX_POINTS,
    }
