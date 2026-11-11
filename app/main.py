"""REST API (FastAPI)."""
import hashlib
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from . import pipeline, search, vector_store
from .config import settings
from .db import SessionLocal, init_db
from .models import Document, Invoice, Page, History
from .processing.ingest import SUPPORTED

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app):
    init_db()
    search.sync_vectors()
    pipeline.recover()
    yield


app = FastAPI(title="DocIntel API", version="1.0.0", lifespan=lifespan,
              description="AI document intelligence: upload, extract, validate, correct, search.")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def auth(x_api_key: Optional[str] = Header(None), api_key: Optional[str] = Query(None)):
    if settings.api_key and (x_api_key or api_key) != settings.api_key:
        raise HTTPException(401, "Invalid or missing API key")


def doc_out(d):
    return {"id": d.id, "filename": d.filename, "kind": d.kind, "size_bytes": d.size_bytes, "status": d.status,
            "stage": d.stage, "progress": d.progress, "page_count": d.page_count, "error": d.error, "attempts": d.attempts,
            "duplicate_of": d.duplicate_of, "processing_ms": d.processing_ms,
            "created_at": d.created_at.isoformat(), "updated_at": d.updated_at.isoformat()}


def inv_out(i, full=True):
    o = {"id": i.id, "document_id": i.document_id, "filename": i.document.filename if i.document else None,
         "page_start": i.page_start, "page_end": i.page_end, "status": i.status, "corrected": i.corrected, "method": i.method,
         **{f: getattr(i, f) for f in pipeline.FIELDS}, "issue_count": len(i.issues)}
    if full:
        o.update(line_items=i.line_items or [], confidence=i.confidence or {}, notes=(i.extras or {}).get("notes", []),
                 issues=[{"code": x.code, "severity": x.severity, "field": x.field, "message": x.message} for x in i.issues])
    return o


# ---------- documents ----------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "llm": bool(settings.gemini_api_key),
        "llm_provider": "gemini",
        "vector_store": "chroma",
        "embedding_backend": settings.embedding_backend,
    }


@app.post("/api/documents", dependencies=[Depends(auth)], status_code=202)
async def upload(files: List[UploadFile] = File(...), db=Depends(get_db)):
    """Accepts many files. Bad files are reported per-file; the rest are queued for background processing."""
    accepted, rejected = [], []
    for f in files:
        name = Path(f.filename or "unnamed").name
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED:
            rejected.append({"filename": name, "error": f"Unsupported type '{ext}'"}); continue
        dest, h, size = settings.upload_dir / f"{uuid.uuid4().hex}{ext}", hashlib.sha256(), 0
        too_big = False
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                size += len(chunk); h.update(chunk); out.write(chunk)
                if size > settings.max_upload_mb * 1 << 20:
                    too_big = True; break
        if too_big or size == 0:
            dest.unlink(missing_ok=True)
            rejected.append({"filename": name, "error": "File is empty" if size == 0 else f"Exceeds {settings.max_upload_mb} MB"}); continue
        sha = h.hexdigest()
        prior = db.query(Document).filter(Document.sha256 == sha, Document.status != "failed").order_by(Document.id).first()
        doc = Document(filename=name, stored_path=str(dest), sha256=sha, size_bytes=size)
        if prior:
            dest.unlink(missing_ok=True); doc.stored_path = prior.stored_path
            doc.status, doc.stage, doc.duplicate_of, doc.progress, doc.kind = "duplicate", "duplicate_file", prior.id, 100, prior.kind
            doc.error = f"Identical file already uploaded as document #{prior.id}; not re-processed"
        db.add(doc); db.flush()
        pipeline.add_history(db, doc.id, "uploaded", {"size": size, "sha256": sha, "duplicate_of": doc.duplicate_of})
        db.commit(); accepted.append(doc)
        if not prior:
            pipeline.enqueue(doc.id)
    return {"documents": [doc_out(d) for d in accepted], "rejected": rejected}


@app.get("/api/documents", dependencies=[Depends(auth)])
def list_documents(status: Optional[str] = None, q: Optional[str] = None, limit: int = Query(50, le=500), offset: int = 0, db=Depends(get_db)):
    qs = db.query(Document)
    if status: qs = qs.filter(Document.status == status)
    if q: qs = qs.filter(Document.filename.ilike(f"%{q}%"))
    total = qs.count()
    return {"total": total, "items": [doc_out(d) for d in qs.order_by(Document.id.desc()).offset(offset).limit(limit)]}


def _doc(db, doc_id):
    d = db.get(Document, doc_id)
    if not d: raise HTTPException(404, "Document not found")
    return d


@app.get("/api/documents/{doc_id}", dependencies=[Depends(auth)])
def get_document(doc_id: int, db=Depends(get_db)):
    d = _doc(db, doc_id)
    pages = db.query(Page.page_no, Page.source, Page.ocr_confidence, Page.error).filter(Page.document_id == d.id).order_by(Page.page_no).all()
    return {**doc_out(d), "records": [inv_out(i, full=False) for i in d.invoices],
            "pages": [{"page_no": p[0], "source": p[1], "ocr_confidence": p[2], "error": p[3]} for p in pages[:2000]]}


@app.get("/api/documents/{doc_id}/pages/{page_no}", dependencies=[Depends(auth)])
def get_page(doc_id: int, page_no: int, db=Depends(get_db)):
    p = db.query(Page).filter(Page.document_id == doc_id, Page.page_no == page_no).first()
    if not p: raise HTTPException(404, "Page not found")
    return {"page_no": p.page_no, "source": p.source, "ocr_confidence": p.ocr_confidence, "text": p.text}


@app.get("/api/documents/{doc_id}/history", dependencies=[Depends(auth)])
def history(doc_id: int, db=Depends(get_db)):
    _doc(db, doc_id)
    rows = db.query(History).filter(History.document_id == doc_id).order_by(History.id).all()
    return [{"ts": h.ts.isoformat(), "event": h.event, "invoice_id": h.invoice_id, "detail": h.detail} for h in rows]


@app.get("/api/documents/{doc_id}/file", dependencies=[Depends(auth)])
def download(doc_id: int, db=Depends(get_db)):
    d = _doc(db, doc_id)
    return FileResponse(d.stored_path, filename=d.filename)


@app.post("/api/documents/{doc_id}/reprocess", dependencies=[Depends(auth)], status_code=202)
def reprocess(doc_id: int, force: bool = False, db=Depends(get_db)):
    d = _doc(db, doc_id)
    if d.status == "processing": raise HTTPException(409, "Document is currently processing")
    if d.status == "queued": raise HTTPException(409, "Document is already queued")
    if not force and any(i.corrected for i in d.invoices):
        raise HTTPException(409, "Document has manually corrected records; pass force=true to overwrite them")
    d.status, d.stage, d.progress, d.error = "queued", "queued", 0, None
    pipeline.add_history(db, d.id, "reprocess_requested", {"force": force}); db.commit()
    pipeline.enqueue(d.id)
    return doc_out(d)


# ---------- records ----------
@app.get("/api/invoices", dependencies=[Depends(auth)])
def list_invoices(q: Optional[str] = None, supplier: Optional[str] = None, customer: Optional[str] = None,
                  invoice_number: Optional[str] = None, status: Optional[str] = None, currency: Optional[str] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None, min_total: Optional[float] = None,
                  max_total: Optional[float] = None, document_id: Optional[int] = None,
                  limit: int = Query(50, le=500), offset: int = 0, db=Depends(get_db)):
    qs = search.filter_query(db, q, supplier, customer, invoice_number, status, currency, date_from, date_to, min_total, max_total, document_id)
    return {"total": qs.count(), "items": [inv_out(i, full=False) for i in qs.order_by(Invoice.id.desc()).offset(offset).limit(limit)]}


def _inv(db, inv_id):
    i = db.get(Invoice, inv_id)
    if not i: raise HTTPException(404, "Record not found")
    return i


@app.get("/api/invoices/{inv_id}", dependencies=[Depends(auth)])
def get_invoice(inv_id: int, db=Depends(get_db)):
    i = _inv(db, inv_id)
    return {**inv_out(i), "source_text": i.source_text}


def _update_document_status(db, document_id):
    has_pending_review = db.query(Invoice.id).filter(
        Invoice.document_id == document_id,
        Invoice.status == "needs_review",
    ).first()
    document = db.get(Document, document_id)
    document.status = "needs_review" if has_pending_review else "completed"


@app.post("/api/invoices/{inv_id}/review", dependencies=[Depends(auth)])
def review_invoice(inv_id: int, db=Depends(get_db)):
    """Acknowledge review without changing extracted values or removing validation findings."""
    i = _inv(db, inv_id)
    new_status = "corrected" if i.corrected else "ok"
    if i.status != new_status:
        i.status = new_status
        pipeline.add_history(db, i.document_id, "reviewed", {"status": new_status}, invoice_id=i.id)
    _update_document_status(db, i.document_id)
    db.commit()
    return inv_out(i)


class LineItemIn(BaseModel):
    description: str
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


class Correction(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    supplier: Optional[str] = None
    customer: Optional[str] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = None
    line_items: Optional[List[LineItemIn]] = None

    @field_validator("invoice_date")
    @classmethod
    def _date(cls, v):
        if v in (None, ""): return None
        try: datetime.strptime(v, "%Y-%m-%d")
        except ValueError: raise ValueError("invoice_date must be YYYY-MM-DD")
        return v

    @field_validator("currency")
    @classmethod
    def _cur(cls, v):
        if v in (None, ""): return None
        if not re.fullmatch(r"[A-Za-z]{3}", v): raise ValueError("currency must be a 3-letter ISO code")
        return v.upper()

    @field_validator("subtotal", "tax", "total")
    @classmethod
    def _num(cls, v):
        if v is not None and (v != v or abs(v) > 1e12): raise ValueError("invalid amount")
        return v


@app.patch("/api/invoices/{inv_id}", dependencies=[Depends(auth)])
def correct(inv_id: int, body: Correction, db=Depends(get_db)):
    """Manual correction. Only fields present in the body change (send null to clear). Re-validates and re-indexes."""
    i = _inv(db, inv_id)
    changes, conf = {}, dict(i.confidence or {})
    for k in body.model_fields_set:
        new = body.line_items and [x.model_dump() for x in body.line_items] if k == "line_items" else getattr(body, k)
        if k == "line_items" and body.line_items is None: new = []
        old = getattr(i, k)
        if new != old:
            changes[k] = {"from": old, "to": new}; setattr(i, k, new)
            if k != "line_items": conf[k] = 1.0
    if not changes: return inv_out(i)
    i.confidence = conf
    ex = dict(i.extras or {}); ex["invalid"] = {k: v for k, v in (ex.get("invalid") or {}).items() if k not in changes}
    ex["dropped"] = [k for k in ex.get("dropped", []) if k not in changes]; i.extras = ex
    i.corrected, i.method = True, i.method or "manual"
    db.flush(); pipeline.revalidate(db, i)
    pipeline.add_history(db, i.document_id, "corrected", changes, invoice_id=i.id)
    _update_document_status(db, i.document_id)
    vector_record = (i.id, search.record_text(pipeline.inv_to_rec(i)))
    vector_store.upsert([vector_record])
    db.commit()
    return inv_out(i)


@app.get("/api/search", dependencies=[Depends(auth)])
def search_api(q: str = Query(..., min_length=1), mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
               k: int = Query(20, le=100), supplier: Optional[str] = None, status: Optional[str] = None, db=Depends(get_db)):
    filters = {"supplier": supplier, "status": status}
    if mode == "keyword":
        res = [(i.id, 1.0, "keyword") for i in search.filter_query(db, q=q, **filters).limit(k)]
    elif mode == "semantic":
        allowed = {i.id for i in search.filter_query(db, **filters).with_entities(Invoice.id)} if any(filters.values()) else None
        res = [(i, round(s, 4), "semantic") for i, s in search.semantic(db, q, k, allowed)]
    else:
        res = search.hybrid(db, q, k, **filters)
    out = []
    for iid, score, via in res:
        inv = db.get(Invoice, iid)
        if inv: out.append({**inv_out(inv, full=False), "score": score, "matched_by": via})
    return {"query": q, "mode": mode, "results": out}


@app.get("/api/stats", dependencies=[Depends(auth)])
def stats(db=Depends(get_db)):
    by = dict(db.query(Document.status, func.count()).group_by(Document.status).all())
    done = by.get("completed", 0) + by.get("needs_review", 0); failed = by.get("failed", 0)
    return {"documents": by, "total_documents": sum(by.values()),
            "records": db.query(func.count(Invoice.id)).scalar(), "records_needing_review": db.query(func.count(Invoice.id)).filter(Invoice.status == "needs_review").scalar(),
            "success_rate": round(done / (done + failed), 4) if done + failed else None,
            "avg_processing_ms": int(db.query(func.avg(Document.processing_ms)).filter(Document.processing_ms.isnot(None)).scalar() or 0),
            "pages_processed": db.query(func.coalesce(func.sum(Document.page_count), 0)).scalar()}


if FRONTEND.exists():
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(FRONTEND / "index.html")
