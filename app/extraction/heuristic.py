"""Deterministic, offline extractor. Used (a) as fallback with no LLM key, (b) to cross-check LLM output.
Returns {field: (value, confidence)} + line_items. Never guesses: unknown -> (None, 0)."""
import re
from ..utils import DATE_RE, parse_amount, to_iso_date
from ..config import settings

NUM = r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?!\s*%)(?!\d)"
INV_RE = re.compile(r"(?:invoice|inv|bill|receipt)\s*(?:no\.?|number|num|#|id)\s*[:#.\-]?\s*([A-Za-z0-9][A-Za-z0-9\-_/]{2,30})", re.I)
INV_RE2 = re.compile(r"\binvoice\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9\-_/]{2,30})", re.I)
PARTY_STOP = re.compile(r"\s{2,}|\b(?:bill(?:ed)?\s*to|ship\s*to|from|invoice|date|customer|client|supplier|vendor)\b\s*:", re.I)
SUPPLIER_L = r"(?:from|supplier|vendor|seller|billed\s*by|issued\s*by|sold\s*by|payee)"
CUSTOMER_L = r"(?:bill(?:ed)?\s*to|customer|client|sold\s*to|buyer|invoice\s*to|payer)"
ITEM_RE = re.compile(r"^(?P<d>.*[A-Za-z].*?)\s+(?P<q>\d+(?:\.\d+)?)\s+(?:[x×]\s*)?[$€£৳]?\s?(?P<u>\d[\d,]*\.\d{1,2})\s+[$€£৳]?\s?(?P<a>\d[\d,]*\.\d{1,2})\s*$")
ISO_CODES = "USD EUR GBP BDT INR AED CAD AUD JPY CNY SGD CHF SAR MYR".split()


def find_invoice_number(text):
    text = text.replace(" | ", "  ")
    for rx in (INV_RE, INV_RE2):
        for m in rx.finditer(text):
            v = m.group(1).strip("-_/.")
            if re.search(r"\d", v) and v.lower() not in ("date", "number"):
                return v
    return None


def _party(text, label):
    lines = text.split("\n")
    rx = re.compile(rf"\b{label}\b\s*[:\-]?\s*(.*)$", re.I)
    for i, ln in enumerate(lines):
        m = rx.search(ln)
        if not m:
            continue
        val = m.group(1)
        if not val.strip():  # value on the next non-empty line
            val = next((l for l in lines[i + 1:i + 3] if l.strip()), "")
        val = PARTY_STOP.split(val.strip())[0].strip(" :-,")
        if len(val) >= 2 and not re.fullmatch(r"[\d\W]+", val):
            return val
    return None


def _amount_on(line, after=0):
    nums = re.findall(NUM, line[after:])
    return parse_amount(nums[-1]) if nums else None


def extract(text):
    text = text.replace(" | ", "  ")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = {k: (None, 0.0) for k in
           ("invoice_number", "invoice_date", "supplier", "customer", "subtotal", "tax", "total", "currency")}
    notes = []
    v = find_invoice_number(text)
    if v: out["invoice_number"] = (v, 0.9)

    # date: prefer labelled invoice date, then any 'date' label (not due/ship), then first date in header
    for rx in (r"invoice\s*date|date\s*of\s*issue|issue\s*date|billing\s*date|dated?", ):
        for m in re.finditer(rf"(?<!due\s)(?<!ship\s)(?<!delivery\s)\b(?:{rx})\b\s*[:\-]?\s*", text, re.I):
            d = DATE_RE.search(text[m.end():m.end() + 40])
            if d:
                iso, amb = to_iso_date(d.group(1), settings.dayfirst)
                if iso:
                    out["invoice_date"] = (iso, 0.6 if amb else 0.9)
                    if amb: notes.append(f"date '{d.group(1)}' is ambiguous (day/month order assumed)")
                    break
        if out["invoice_date"][0]:
            break
    if not out["invoice_date"][0]:
        for l in lines[:15]:
            d = DATE_RE.search(l)
            if d and not re.search(r"due", l, re.I):
                iso, amb = to_iso_date(d.group(1), settings.dayfirst)
                if iso:
                    out["invoice_date"] = (iso, 0.5); break

    s = _party(text, SUPPLIER_L)
    if s: out["supplier"] = (s, 0.85)
    else:  # letterhead fallback: first line that is not a title/number-heavy
        for n, l in enumerate(lines[:4]):   # letterhead guess: first line is usually the issuer, later lines are less certain
            if len(l) > 2 and not re.search(r"invoice|receipt|tax|bill|page|date|\d{3,}", l, re.I):
                out["supplier"] = (l[:120], 0.7 if n == 0 else 0.5); break
    c = _party(text, CUSTOMER_L)
    if c: out["customer"] = (c, 0.85)

    sub = tax = tot = tot_strong = None
    for l in lines:
        low = l.lower()
        if re.search(r"\bsub\s*-?\s*total\b", low):
            a = _amount_on(l, re.search(r"sub\s*-?\s*total", low).end()); sub = a if a is not None else sub
        elif re.search(r"\b(tax|vat|gst)\b", low) and not re.search(r"\b(id|no|number|reg|registration|bin|tin)\b", low):
            m = re.search(r"\b(tax|vat|gst)\b", low); a = _amount_on(l, m.end()); tax = a if a is not None else tax
        elif re.search(r"grand\s*total|total\s*(amount|due|payable)|amount\s*(due|payable)|balance\s*due|^total\b", low):
            m = re.search(r"grand\s*total|total\s*(amount|due|payable)|amount\s*(due|payable)|balance\s*due|total", low)
            a = _amount_on(l, m.end())
            if a is not None:
                strong = not low.startswith("total") or "amount" in low or "due" in low
                if strong or not tot_strong: tot = a; tot_strong = tot_strong or strong
    for k, val in (("subtotal", sub), ("tax", tax), ("total", tot)):
        if val is not None: out[k] = (val, 0.85)

    # currency
    m = re.search(r"\b(" + "|".join(ISO_CODES) + r")\b", text)
    if m: out["currency"] = (m.group(1), 0.9)
    elif re.search(r"৳|\bTk\.?\b", text): out["currency"] = ("BDT", 0.8)
    elif "€" in text: out["currency"] = ("EUR", 0.8)
    elif "£" in text: out["currency"] = ("GBP", 0.8)
    elif "$" in text: out["currency"] = ("USD", 0.6)

    items = []
    for l in lines:
        if re.search(r"sub\s*-?\s*total|\btotal\b|\btax\b|\bvat\b", l, re.I):
            continue
        m = ITEM_RE.match(l)
        if m:
            items.append({"description": re.sub(r"^\d+[.)]?\s+(?=[A-Za-z])", "", m["d"]).strip(),
                          "quantity": float(m["q"]), "unit_price": parse_amount(m["u"]), "amount": parse_amount(m["a"])})
    return {"fields": out, "line_items": items, "notes": notes}
