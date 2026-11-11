"""Gemini LLM extractor. Output is parsed defensively and grounded against the source text."""
import json
import re
from .prompts import SYSTEM_PROMPT
from ..config import settings
from ..utils import parse_amount, to_iso_date, norm

MAX_CHARS = 24000
_client = None


def available():
    return bool(settings.gemini_api_key)


def get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def call_llm(text):
    from google.genai import types
    response = get_client().models.generate_content(
        model=settings.llm_model,
        contents=f"<document>\n{text[:MAX_CHARS]}\n</document>",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=2000,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    return response.text or ""


def parse_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else None


def _numbers_in(text):
    vals = set()
    for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        v = parse_amount(t)
        if v is not None: vals.add(v)
    return vals


def ground(data, text):
    """Drop any LLM value that cannot be found in the source text (anti-hallucination guard)."""
    nums, ntext = _numbers_in(text), norm(text)
    dropped = []
    for k in ("subtotal", "tax", "total"):
        v = parse_amount(data.get(k))
        if v is not None and not any(abs(v - n) <= 0.005 for n in nums):
            dropped.append(k); v = None
        data[k] = v
    for k in ("invoice_number", "supplier", "customer"):
        v = data.get(k)
        if v is not None and norm(str(v)) not in ntext:
            dropped.append(k); data[k] = None
    if data.get("invoice_date"):
        iso, _ = to_iso_date(data["invoice_date"], settings.dayfirst)
        data["invoice_date"] = iso
        if iso:
            y, mo, d = iso.split("-")
            if y not in text and y[2:] not in text: dropped.append("invoice_date"); data["invoice_date"] = None
    items = []
    for it in data.get("line_items") or []:
        if not isinstance(it, dict) or not it.get("description"): continue
        amt = parse_amount(it.get("amount"))
        if amt is not None and not any(abs(amt - n) <= 0.005 for n in nums): amt = None
        items.append({"description": str(it["description"]), "quantity": parse_amount(it.get("quantity")),
                      "unit_price": parse_amount(it.get("unit_price")), "amount": amt})
    data["line_items"] = items
    return data, dropped


def extract(text, caller=None):
    """Returns {'fields': {k:(v,conf)}, 'line_items', 'notes'} or None when unavailable/failed."""
    try:
        raw = (caller or call_llm)(text)
        data = parse_json(raw)
        if not isinstance(data, dict): return None
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    data, dropped = ground(data, text)
    conf = data.get("confidence") or {}
    fields = {}
    for k in ("invoice_number", "invoice_date", "supplier", "customer", "subtotal", "tax", "total", "currency"):
        v = data.get(k)
        c = conf.get(k, 0.7)
        fields[k] = (v, float(c) if v is not None and isinstance(c, (int, float)) else 0.0)
    notes = list(data.get("notes") or [])
    if dropped: notes.append("LLM values not found in source text were discarded: " + ", ".join(dropped))
    return {"fields": fields, "line_items": data["line_items"], "notes": notes, "dropped": dropped}
