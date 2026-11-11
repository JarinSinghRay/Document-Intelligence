import pytest
from app.utils import parse_amount, to_iso_date
from app.validation import validate
from app.extraction import heuristic, llm
from app.extraction.extractor import extract_text, merge
from app.extraction.segment import segment_pages
from app.config import settings

@pytest.mark.parametrize("raw,exp", [("$1,234.50", 1234.5), ("1.234,50", 1234.5), ("12,50", 12.5), ("1,234", 1234.0), ("(12.00)", -12.0), ("N/A", None), ("", None), ("৳ 5,000", 5000.0)])
def test_parse_amount(raw, exp): assert parse_amount(raw) == exp

@pytest.mark.parametrize("raw,exp", [("2025-05-10", "2025-05-10"), ("15/03/2025", "2025-03-15"), ("03/15/2025", "2025-03-15"), ("March 5, 2025", "2025-03-05"), ("5 Mar 2025", "2025-03-05"), ("garbage", None)])
def test_dates(raw, exp): assert to_iso_date(raw)[0] == exp

def test_iso_date_not_swapped():  # regression: dayfirst=True used to turn 2025-05-10 into 2025-10-05
    assert to_iso_date("2025-05-10", dayfirst=True) == ("2025-05-10", False)

def test_ambiguous_date_flagged(): assert to_iso_date("04/05/2025")[1] is True

BASE = dict(invoice_number="X1", invoice_date="2025-01-02", supplier="Acme", customer="C", currency="USD", subtotal=100.0, tax=10.0, total=110.0,
            line_items=[{"description": "a", "quantity": 2, "unit_price": 50, "amount": 100}], confidence={}, extras={})
codes = lambda r, **kw: {i["code"] for i in validate(r, **kw)}

def test_valid_record_has_no_issues(): assert validate(BASE) == []
def test_missing_required():
    r = {**BASE, "invoice_number": None, "total": None}; assert "missing_field" in codes(r)
def test_total_mismatch_small_error_detected(): assert "total_mismatch" in codes({**BASE, "total": 120.0})
def test_total_mismatch_tolerates_rounding(): assert "total_mismatch" not in codes({**BASE, "total": 110.01})
def test_line_math(): assert "line_item_math" in codes({**BASE, "line_items": [{"description": "a", "quantity": 2, "unit_price": 50, "amount": 107}]})
def test_items_vs_subtotal(): assert "items_vs_subtotal" in codes({**BASE, "subtotal": 90.0, "total": 100.0})
def test_negative_and_future():
    c = codes({**BASE, "total": -110.0, "subtotal": -100.0, "tax": -10.0, "invoice_date": "2099-01-01"}); assert {"negative_amount", "future_date"} <= c
def test_invalid_raw_value(): assert "invalid_value" in codes({**BASE, "extras": {"invalid": {"total": "N/A"}}})
def test_duplicate_callback(): assert "duplicate_invoice" in codes(BASE, find_duplicates=lambda s, n: [7])
def test_low_confidence(): assert "low_confidence" in codes({**BASE, "confidence": {"supplier": 0.3}})
def test_unverifiable_total(): assert "unverifiable_total" in codes({**BASE, "subtotal": None, "line_items": [], "tax": None})

TEXT = "Acme Ltd\nInvoice No: INV-77\nInvoice Date: 2025-02-03\nFrom: Acme Ltd\nBill To: Beta Inc\nDescription Qty Unit Price Amount\nWidget 2 10.00 20.00\nSubtotal 20.00\nTax (10%) 2.00\nTotal $22.00"
def test_heuristic_extracts():
    r = heuristic.extract(TEXT); f = {k: v[0] for k, v in r["fields"].items()}
    assert f["invoice_number"] == "INV-77" and f["total"] == 22.0 and f["tax"] == 2.0 and f["customer"] == "Beta Inc" and f["invoice_date"] == "2025-02-03"
    assert r["line_items"] == [{"description": "Widget", "quantity": 2.0, "unit_price": 10.0, "amount": 20.0}]
def test_heuristic_never_invents():
    f = heuristic.extract("some random text with no invoice data 12")["fields"]
    assert f["invoice_number"][0] is None and f["total"][0] is None and f["customer"][0] is None and f["invoice_date"][0] is None
def test_two_column_party_split():
    f = heuristic.extract("From: Acme Ltd  Bill To: Beta Inc\nInvoice No: A-1001")["fields"]
    assert f["supplier"][0] == "Acme Ltd" and f["customer"][0] == "Beta Inc"

def test_llm_hallucination_dropped():
    fake = lambda t: '{"invoice_number":"INV-77","supplier":"Totally Made Up Corp","total":999.99,"subtotal":20,"tax":2,"invoice_date":"2025-02-03","customer":"Beta Inc","currency":"USD","line_items":[],"confidence":{"total":0.99}}'
    rec, method = extract_text(TEXT, llm_caller=fake); f = rec["fields"]
    assert method == "llm+heuristic" and f["total"][0] == 22.0           # hallucinated 999.99 discarded -> rules value used
    assert f["supplier"][0] == "Acme Ltd" and "total" in rec["dropped"] and "supplier" in rec["dropped"]
def test_llm_markdown_fenced_json_parsed():
    out = llm.extract(TEXT, lambda t: '```json\n{"invoice_number":"INV-77","total":22,"line_items":[]}\n```'); assert out["fields"]["invoice_number"][0] == "INV-77"

def test_gemini_request_uses_configured_model_and_json(monkeypatch):
    class Models:
        def generate_content(self, **kwargs):
            self.kwargs = kwargs
            return type("Response", (), {"text": '{"invoice_number":"INV-77"}'})()
    class Client:
        models = Models()
    fake = Client()
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    assert llm.call_llm(TEXT) == '{"invoice_number":"INV-77"}'
    assert fake.models.kwargs["model"] == settings.llm_model
    assert fake.models.kwargs["config"].response_mime_type == "application/json"
def test_llm_failure_falls_back():
    def boom(t): raise RuntimeError("api down")
    rec, method = extract_text(TEXT, llm_caller=boom); assert method == "heuristic" and rec["fields"]["total"][0] == 22.0 and any("unavailable" in n for n in rec["notes"])
def test_llm_garbage_falls_back():
    rec, method = extract_text(TEXT, llm_caller=lambda t: "I cannot help"); assert method == "heuristic"
def test_disagreement_lowers_confidence():
    h = heuristic.extract(TEXT); l = {"fields": {**h["fields"], "total": (23.0, 0.9)}, "line_items": [], "notes": []}
    m = merge(h, l); assert m["fields"]["total"][1] <= 0.55 and any("disagree" in n for n in m["notes"])
def test_segmentation_splits_on_invoice_number():
    pages = [{"page_no": 1, "text": "Invoice No: A-001"}, {"page_no": 2, "text": "Invoice No: A-001 (cont)"}, {"page_no": 3, "text": "Invoice No: B-002"}]
    s = segment_pages(pages); assert [(x["page_start"], x["page_end"]) for x in s] == [(1, 2), (3, 3)]
