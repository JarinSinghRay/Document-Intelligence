"""Business-rule validation. Pure function over a record; DB lookups injected for duplicate checks."""
from datetime import date
from .config import settings
from .utils import norm

REQUIRED_ERR = ("invoice_number", "invoice_date", "supplier", "total")
REQUIRED_WARN = ("customer", "currency")


def _close(a, b, extra=0.0):
    """Absolute tolerance only (rounding noise is cents; a % tolerance would hide real errors). `extra` allows per-line rounding."""
    return abs(a - b) <= settings.amount_tolerance + extra + 1e-9


def validate(rec, find_duplicates=None, find_similar=None):
    """rec: dict with invoice fields, line_items, confidence, extras. -> list of issue dicts."""
    I = []
    add = lambda code, sev, field, msg: I.append({"code": code, "severity": sev, "field": field, "message": msg})
    ex = rec.get("extras") or {}
    for f in REQUIRED_ERR:
        if rec.get(f) in (None, ""): add("missing_field", "error", f, f"Required field '{f}' is missing")
    for f in REQUIRED_WARN:
        if rec.get(f) in (None, ""): add("missing_field", "warning", f, f"Field '{f}' is missing")
    for f, raw in (ex.get("invalid") or {}).items():
        add("invalid_value", "error", f, f"'{raw}' is not a valid value for {f}")
    sub, tax, tot = rec.get("subtotal"), rec.get("tax"), rec.get("total")
    for f, v in (("subtotal", sub), ("tax", tax), ("total", tot)):
        if v is not None and v < 0: add("negative_amount", "warning", f, f"{f} is negative ({v}); credit note?")
    if tot == 0: add("zero_total", "warning", "total", "Total is zero")
    d = rec.get("invoice_date")
    if d:
        if d > date.today().isoformat(): add("future_date", "warning", "invoice_date", f"Invoice date {d} is in the future")
        elif d < "2000-01-01": add("implausible_date", "warning", "invoice_date", f"Invoice date {d} is implausibly old")
    items = rec.get("line_items") or []
    for n, it in enumerate(items, 1):
        q, u, a = it.get("quantity"), it.get("unit_price"), it.get("amount")
        if None not in (q, u, a) and not _close(q * u, a, 0.0005 * abs(a)):
            add("line_item_math", "error", f"line_items[{n}]", f"Line {n}: {q} x {u} = {round(q*u,2)} but amount is {a}")
        if a is None: add("line_item_amount_missing", "warning", f"line_items[{n}]", f"Line {n} has no amount")
    isum = round(sum(i["amount"] for i in items if i.get("amount") is not None), 2) if items else None
    if isum is not None and sub is not None and not _close(isum, sub, 0.01 * len(items)):
        add("items_vs_subtotal", "error", "subtotal", f"Line items sum to {isum} but subtotal is {sub}")
    base = sub if sub is not None else isum
    if base is not None and tot is not None:
        expected = round(base + (tax or 0), 2)
        if not _close(expected, tot):
            add("total_mismatch", "error", "total", f"{'subtotal' if sub is not None else 'items'} {base} + tax {tax or 0} = {expected}, but total is {tot}")
    elif tot is not None and base is None:
        add("unverifiable_total", "warning", "total", "No subtotal or line items available to verify the total")
    if tax is not None and base and (tax / base > 0.5): add("implausible_tax", "warning", "tax", f"Tax is {tax/base:.0%} of the base amount")
    for f, c in (rec.get("confidence") or {}).items():
        if f.startswith("_"): continue
        if rec.get(f) is not None and c < settings.review_confidence:
            add("low_confidence", "warning", f, f"Low confidence ({c:.2f}) for '{f}' - please verify")
    for f in ex.get("dropped") or []:
        add("ungrounded_value", "warning", f, f"AI proposed a value for '{f}' that is not present in the document; discarded")
    for n in ex.get("notes") or []:
        if "ambiguous" in n: add("ambiguous_date", "warning", "invoice_date", n)
    if find_duplicates and rec.get("invoice_number") and rec.get("supplier"):
        dups = find_duplicates(rec["supplier"], rec["invoice_number"])
        if dups: add("duplicate_invoice", "error", "invoice_number", f"Same supplier + invoice number already exists (record(s) {', '.join(map(str, dups))})")
    if find_similar and not any(i["code"] == "duplicate_invoice" for i in I) and None not in (rec.get("supplier"), tot, d):
        sim = find_similar(rec["supplier"], tot, d, rec.get("invoice_number"))
        if sim: add("possible_duplicate", "warning", "total", f"Same supplier, date and total as record(s) {', '.join(map(str, sim))} with a different invoice number")
    return I
