"""Keyword filtering (SQL) + Chroma semantic search + hybrid (RRF)."""
from sqlalchemy import or_
from .models import Invoice
from . import embeddings
from . import vector_store


def filter_query(db, q=None, supplier=None, customer=None, invoice_number=None, status=None, currency=None,
                 date_from=None, date_to=None, min_total=None, max_total=None, document_id=None):
    qs = db.query(Invoice)
    if q:
        like = f"%{q}%"
        qs = qs.filter(or_(Invoice.supplier.ilike(like), Invoice.customer.ilike(like), Invoice.invoice_number.ilike(like),
                           Invoice.source_text.ilike(like)))
    if supplier: qs = qs.filter(Invoice.supplier.ilike(f"%{supplier}%"))
    if customer: qs = qs.filter(Invoice.customer.ilike(f"%{customer}%"))
    if invoice_number: qs = qs.filter(Invoice.invoice_number.ilike(f"%{invoice_number}%"))
    if status: qs = qs.filter(Invoice.status == status)
    if currency: qs = qs.filter(Invoice.currency == currency.upper())
    if date_from: qs = qs.filter(Invoice.invoice_date >= date_from)
    if date_to: qs = qs.filter(Invoice.invoice_date <= date_to)
    if min_total is not None: qs = qs.filter(Invoice.total >= min_total)
    if max_total is not None: qs = qs.filter(Invoice.total <= max_total)
    if document_id: qs = qs.filter(Invoice.document_id == document_id)
    return qs


def semantic(db, q, k=20, allowed_ids=None):
    vector = embeddings.embed([q])[0].tolist()
    return [(invoice_id, score) for invoice_id, score in vector_store.query(vector, k, allowed_ids)
            if score > 0.05]


def sync_vectors():
    from .db import SessionLocal
    with SessionLocal() as db:
        invoices = db.query(Invoice).all()
        records = [(invoice.id, record_text({
            "supplier": invoice.supplier,
            "customer": invoice.customer,
            "invoice_number": invoice.invoice_number,
            "currency": invoice.currency,
            "line_items": invoice.line_items or [],
            "source_text": invoice.source_text,
        })) for invoice in invoices]
    return vector_store.sync(records)


def upsert_vectors(records):
    vector_store.upsert(records)


def delete_vectors(invoice_ids):
    vector_store.delete(invoice_ids)


def record_text(rec):
    parts = [rec.get("supplier"), rec.get("customer"), rec.get("invoice_number"), rec.get("currency"),
             " ".join(i.get("description", "") for i in rec.get("line_items") or [])]
    return " ".join(p for p in parts if p) + "\n" + (rec.get("source_text") or "")[:1500]


def hybrid(db, q, k=20, **filters):
    kw = [i.id for i in filter_query(db, q=q, **filters).limit(200).all()]
    allowed = {i.id for i in filter_query(db, **filters).with_entities(Invoice.id).all()} if any(filters.values()) else None
    sem = semantic(db, q, k=100, allowed_ids=allowed)
    score = {}
    for r, i in enumerate(kw): score[i] = score.get(i, 0) + 1 / (60 + r)
    for r, (i, _) in enumerate(sem): score[i] = score.get(i, 0) + 1 / (60 + r)
    sem_map = dict(sem)
    ranked = sorted(score, key=score.get, reverse=True)[:k]
    return [(i, round(score[i] * 60 / 2, 4), "keyword+semantic" if i in kw and i in sem_map else ("keyword" if i in kw else "semantic")) for i in ranked]
