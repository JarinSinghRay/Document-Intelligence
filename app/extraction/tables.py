"""Structured spreadsheet extraction: maps header synonyms to invoice fields. Handles one-row-per-invoice
and long format (repeating invoice numbers = line items)."""
from ..utils import norm, parse_amount, to_iso_date, DATE_RE
from ..config import settings

SYN = {
    "invoice_number": ["invoice number", "invoice no", "invoice", "invoice id", "inv no", "inv number", "inv"],
    "invoice_date": ["invoice date", "date", "issue date", "inv date"],
    "supplier": ["supplier", "vendor", "supplier name", "vendor name", "seller", "from"],
    "customer": ["customer", "customer name", "client", "bill to", "buyer"],
    "subtotal": ["subtotal", "sub total", "net amount", "net total"],
    "tax": ["tax", "vat", "gst", "tax amount", "vat amount"],
    "total": ["total", "grand total", "total amount", "invoice total", "amount due"],
    "currency": ["currency", "curr"],
    "description": ["description", "item", "item description", "product", "particulars"],
    "quantity": ["quantity", "qty"],
    "unit_price": ["unit price", "price", "rate", "unit cost"],
    "amount": ["amount", "line total", "line amount"],
}
LOOKUP = {s: f for f, ss in SYN.items() for s in ss}
NUMERIC = ("subtotal", "tax", "total", "quantity", "unit_price", "amount")


def _find_header(df):
    for i in range(min(30, len(df))):
        m = {}
        for j, c in enumerate(df.iloc[i].tolist()):
            f = LOOKUP.get(norm(c))
            if f and f not in m.values():
                m[j] = f
        if "invoice_number" in m.values() and len(m) >= 3:
            return i, m
    return None, None


def extract_tables(tables):
    """tables: [(sheet_name, DataFrame)] -> list of records (possibly empty -> caller falls back to text)."""
    records = []
    for sheet_idx, (name, df) in enumerate(tables, 1):
        hi, mp = _find_header(df)
        if hi is None:
            continue
        groups, order = {}, []
        for r in range(hi + 1, len(df)):
            row = {f: str(df.iat[r, j]).strip() for j, f in mp.items()}
            if not any(row.values()):
                continue
            key = row.get("invoice_number") or f"__row{r}"
            if key not in groups:
                groups[key] = []; order.append(key)
            groups[key].append(row)
        for key in order:
            rows, first = groups[key], groups[key][0]
            invalid, notes, conf, fields = {}, [], {}, {}
            for f in ("invoice_number", "invoice_date", "supplier", "customer", "currency", "subtotal", "tax", "total"):
                vals = [r.get(f, "") for r in rows if r.get(f, "")]
                raw = vals[0] if vals else ""
                v = None
                if f in NUMERIC:
                    v = parse_amount(raw) if raw else None
                    if raw and v is None: invalid[f] = raw
                elif f == "invoice_date" and raw:
                    d = DATE_RE.search(raw); v, amb = to_iso_date(d.group(1) if d else raw, settings.dayfirst)
                    if raw and not v: invalid[f] = raw
                    if amb: notes.append(f"date '{raw}' is ambiguous (day/month order assumed)")
                elif f == "currency" and raw:
                    v = raw.upper()
                elif raw:
                    v = None if key.startswith("__row") and f == "invoice_number" else raw
                fields[f] = v
                conf[f] = 0.95 if v is not None else 0.0
                if f == "invoice_date" and v and any("ambiguous" in n for n in notes): conf[f] = 0.6
            items = []
            if "description" in mp.values():
                for r in rows:
                    if r.get("description"):
                        items.append({"description": r["description"], "quantity": parse_amount(r.get("quantity")),
                                      "unit_price": parse_amount(r.get("unit_price")), "amount": parse_amount(r.get("amount"))})
            if "total" not in mp.values() and len(rows) == 1 and "amount" in mp.values():
                pass  # never promote 'amount' to total silently
            records.append({"fields": {k: (v, conf[k]) for k, v in fields.items()}, "line_items": items,
                            "notes": notes, "invalid": invalid, "page": sheet_idx, "text": f"{name}: {key}"})
    return records
