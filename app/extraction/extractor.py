"""Combine rule-based + LLM extraction; reconcile; never fabricate."""
from . import heuristic, llm
from ..config import settings

FIELDS = ("invoice_number", "invoice_date", "supplier", "customer", "subtotal", "tax", "total", "currency")


def _agree(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 0.01
    return str(a).strip().lower() == str(b).strip().lower()


def _items_sum(items):
    v = [i["amount"] for i in items if i.get("amount") is not None]
    return round(sum(v), 2) if v else None


def merge(h, l):
    fields, notes = {}, list(h["notes"])
    for k in FIELDS:
        hv, hc = h["fields"][k]
        lv, lc = l["fields"][k]
        if lv is not None and hv is not None:
            if _agree(hv, lv): fields[k] = (lv, 0.97)
            else:
                fields[k] = (lv, min(lc, 0.55)); notes.append(f"LLM and rules disagree on {k}: '{lv}' vs '{hv}'")
        elif lv is not None: fields[k] = (lv, round(lc * 0.9, 2))
        else: fields[k] = (hv, hc)
    notes += l.get("notes", [])
    ref = fields["subtotal"][0] if fields["subtotal"][0] is not None else fields["total"][0]
    cand = [x for x in (l["line_items"], h["line_items"]) if x]
    items = []
    if cand:
        items = min(cand, key=lambda it: abs((_items_sum(it) or 1e18) - ref) if ref is not None else 0)
        if ref is None: items = cand[0]
    return {"fields": fields, "line_items": items, "notes": notes, "dropped": l.get("dropped", [])}


def extract_text(text, llm_caller=None):
    """Returns (record, method). record = {'fields','line_items','notes','dropped'}"""
    h = heuristic.extract(text)
    if llm_caller is None and not llm.available():
        return h, "heuristic"
    l = llm.extract(text, llm_caller)
    if not l or "error" in l:
        h["notes"].append(f"LLM extraction unavailable ({(l or {}).get('error', 'bad JSON')}); rule-based result used")
        return h, "heuristic"
    return merge(h, l), "llm+heuristic"
