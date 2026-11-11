"""Split a long document (e.g. a 1000-page PDF batch) into per-invoice segments by invoice-number changes."""
from .heuristic import find_invoice_number


def segment_pages(pages):
    """pages: [{page_no, text}] -> [{'page_start','page_end','text'}]"""
    segs, cur, cur_no = [], None, None
    for p in pages:
        no = find_invoice_number(p["text"] or "")
        if cur is None or (no and cur_no and no != cur_no):
            cur = {"page_start": p["page_no"], "page_end": p["page_no"], "parts": [p["text"] or ""]}
            segs.append(cur); cur_no = no
        else:
            cur["page_end"] = p["page_no"]; cur["parts"].append(p["text"] or "")
            cur_no = cur_no or no
    return [{"page_start": s["page_start"], "page_end": s["page_end"], "text": "\n".join(s["parts"])} for s in segs]
