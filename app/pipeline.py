"""Background processing pipeline: read -> segment -> extract -> validate -> embed -> persist.
Jobs live in the DB (status column), workers are a thread pool. On restart, unfinished jobs are re-queued.
The queue is behind enqueue(); swap it for Celery/BullMQ/RQ without touching the rest."""
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import func
from . import search
from .config import settings
from .db import SessionLocal
from .extraction import llm
from .extraction.extractor import extract_text
from .extraction.segment import segment_pages
from .extraction.tables import extract_tables
from .models import Document, Page, Invoice, Issue, History
from .processing.errors import ProcessingError
from .processing.ingest import detect_kind, read_document
from .validation import validate

log = logging.getLogger("docintel")
_exec, _exec_lock, _save_lock = None, threading.Lock(), threading.Lock()
FIELDS = ("invoice_number", "invoice_date", "supplier", "customer", "subtotal", "tax", "total", "currency")


def _executor():
    global _exec
    with _exec_lock:
        if _exec is None:
            _exec = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="worker")
        return _exec


def enqueue(doc_id):
    return _executor().submit(process_document, doc_id)


def recover():
    """Re-queue jobs interrupted by a crash/restart."""
    with SessionLocal() as db:
        ids = [d.id for d in db.query(Document).filter(Document.status.in_(["queued", "processing"]))]
        for i in ids:
            db.query(Document).filter(Document.id == i).update({"status": "queued", "stage": "queued"})
        db.commit()
    for i in ids:
        enqueue(i)
    return len(ids)


def add_history(db, doc_id, event, detail=None, invoice_id=None):
    db.add(History(document_id=doc_id, event=event, detail=detail or {}, invoice_id=invoice_id))


def inv_to_rec(inv):
    r = {f: getattr(inv, f) for f in FIELDS}
    r.update(line_items=inv.line_items or [], confidence=inv.confidence or {}, extras=inv.extras or {}, source_text=inv.source_text)
    return r


def revalidate(db, inv):
    """Run all rules for one invoice and (re)write its issues + status. Caller commits."""
    def dups(supplier, number):
        q = db.query(Invoice.id).filter(Invoice.id < (inv.id or 10**12), func.lower(func.trim(Invoice.supplier)) == supplier.strip().lower(),
                                        func.lower(func.trim(Invoice.invoice_number)) == number.strip().lower())
        return [r[0] for r in q.limit(5)]

    def similar(supplier, total, date, number):
        q = db.query(Invoice.id).filter(Invoice.id < (inv.id or 10**12), func.lower(func.trim(Invoice.supplier)) == supplier.strip().lower(),
                                        Invoice.total == total, Invoice.invoice_date == date,
                                        func.lower(func.trim(func.coalesce(Invoice.invoice_number, ""))) != (number or "").strip().lower())
        return [r[0] for r in q.limit(5)]

    issues = validate(inv_to_rec(inv), dups, similar)
    inv.issues = [Issue(**i) for i in issues]
    inv.status = "corrected" if inv.corrected else ("needs_review" if issues else "ok")
    return issues


def _progress(db, doc, stage, pct, force=False, _t=[0.0]):
    doc.stage, doc.progress = stage, round(pct, 1)
    if force or time.time() - _t[0] > 0.4:
        db.commit(); _t[0] = time.time()


def _record_from(seg_text, method_rec=None):
    rec, method = extract_text(seg_text)
    return rec, method


def _scale_conf(conf, ocr_conf):
    """Never let a garbled scan look 'confident': scale by OCR quality (full trust at >= 85)."""
    if ocr_conf is None: return conf
    f = min(1.0, ocr_conf / 85)
    return {k: round(v * f, 3) for k, v in conf.items()}


def build_records(pages, tables, on_progress=None):
    """-> list of dicts ready to persist as Invoice rows."""
    out, covered = [], set()
    pconf = {p["page_no"]: p["ocr_confidence"] for p in pages if p.get("ocr_confidence") is not None}
    if tables:
        for r in extract_tables(tables):
            covered.add(r["page"])
            f = r["fields"]
            out.append({"page_start": r["page"], "page_end": r["page"], "method": "spreadsheet",
                        **{k: f[k][0] for k in FIELDS}, "line_items": r["line_items"],
                        "confidence": {k: f[k][1] for k in FIELDS},
                        "extras": {"notes": r["notes"], "invalid": r["invalid"], "dropped": []},
                        "source_text": r["text"]})
    segs = [s for s in segment_pages([p for p in pages if p["page_no"] not in covered])]
    def work(s):
        try:
            rec, method = extract_text(s["text"])
        except Exception as e:   # extraction must never take the whole file down
            log.exception("extraction failed")
            rec = {"fields": {k: (None, 0.0) for k in FIELDS}, "line_items": [], "notes": [f"Extraction error: {e}"], "dropped": []}
            method = "failed"
        f = rec["fields"]
        cs = [pconf[n] for n in range(s["page_start"], s["page_end"] + 1) if n in pconf]
        oc = sum(cs) / len(cs) if cs else None
        return {"page_start": s["page_start"], "page_end": s["page_end"], "method": method, **{k: f[k][0] for k in FIELDS},
                "line_items": rec["line_items"], "confidence": _scale_conf({k: f[k][1] for k in FIELDS}, oc),
                "extras": {"notes": rec["notes"], "invalid": {}, "dropped": rec.get("dropped", []), "ocr_confidence": round(oc, 1) if oc is not None else None},
                "source_text": s["text"][:30000]}
    if llm.available() and len(segs) > 1:
        with ThreadPoolExecutor(max_workers=8) as ex:
            res = []
            for n, r in enumerate(ex.map(work, segs), 1):
                res.append(r)
                if on_progress: on_progress(n, len(segs))
    else:
        res = []
        for n, s in enumerate(segs, 1):
            res.append(work(s))
            if on_progress and (n % 10 == 0 or n == len(segs)): on_progress(n, len(segs))
    return out + res


def process_document(doc_id):
    t0 = time.time()
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        if doc is None or doc.status == "processing":
            return
        doc.status, doc.stage, doc.progress, doc.error, doc.attempts = "processing", "reading", 1.0, None, (doc.attempts or 0) + 1
        add_history(db, doc.id, "started", {"attempt": doc.attempts}); db.commit()
        try:
            kind = detect_kind(doc.stored_path); doc.kind = kind
            pages, tables = read_document(doc.stored_path, kind,
                                          lambda i, n: _progress(db, doc, f"reading page {i}/{n}", 1 + 59 * i / n, force=(i == n)))
            if not any((p["text"] or "").strip() for p in pages):
                raise ProcessingError("No readable text found (blank, unreadable or too low-quality document)")
            _progress(db, doc, "extracting", 60, force=True)
            records = build_records(pages, tables, lambda i, n: _progress(db, doc, f"extracting {i}/{n}", 60 + 28 * i / n))
            _progress(db, doc, "validating", 90, force=True)
            old_ids = [row[0] for row in db.query(Invoice.id).filter(Invoice.document_id == doc.id).all()]
            with _save_lock:   # serialise validation+insert so duplicate detection is race-free
                db.query(Page).filter(Page.document_id == doc.id).delete()
                db.query(Invoice).filter(Invoice.document_id == doc.id).delete()
                db.flush()
                db.add_all(Page(document_id=doc.id, **p) for p in pages)
                review, vector_records = 0, []
                for n, r in enumerate(records):
                    inv = Invoice(document_id=doc.id, segment_index=n, **r)
                    db.add(inv); db.flush()
                    vector_records.append((inv.id, search.record_text(r)))
                    review += bool(revalidate(db, inv))
                doc.page_count = len(pages)
                failed_pages = [p["page_no"] for p in pages if p["source"] == "failed"]
                doc.status = "needs_review" if (review or failed_pages) else "completed"
                doc.error = f"{len(failed_pages)} page(s) failed to process: {failed_pages[:10]}" if failed_pages else None
                doc.stage, doc.progress = "done", 100.0
                doc.processing_ms = int((time.time() - t0) * 1000)
                add_history(db, doc.id, "completed", {"records": len(records), "needs_review": review, "ms": doc.processing_ms})
                search.upsert_vectors(vector_records)
                db.commit()
            try:
                search.delete_vectors(old_ids)
            except Exception:
                log.exception("Chroma cleanup failed for replaced document %s; startup reconciliation will retry", doc.id)
        except ProcessingError as e:
            _fail(db, doc, str(e), t0)
        except Exception as e:
            log.error(traceback.format_exc())
            _fail(db, doc, f"Unexpected error: {type(e).__name__}: {e}", t0)


def _fail(db, doc, msg, t0):
    db.rollback()
    doc = db.get(Document, doc.id)
    doc.status, doc.stage, doc.error, doc.processing_ms = "failed", "failed", msg, int((time.time() - t0) * 1000)
    add_history(db, doc.id, "failed", {"error": msg}); db.commit()
