"""Persistent Chroma vector index for invoice semantic search."""
import hashlib
import threading

import chromadb

from .config import settings
from .embeddings import embed

COLLECTION_NAME = "invoice_records"
BATCH_SIZE = 256
_lock = threading.RLock()
_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        settings.ensure_dirs()
        client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        _collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def _content_hash(text):
    signature = f"{settings.embedding_backend}:{settings.embedding_model}\0{text}"
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def upsert(records):
    """Insert or update (invoice_id, text) pairs in persistent, batched writes."""
    records = [(str(invoice_id), text) for invoice_id, text in records if text]
    if not records:
        return
    with _lock:
        collection = _get_collection()
        for start in range(0, len(records), BATCH_SIZE):
            batch = records[start:start + BATCH_SIZE]
            ids, texts = zip(*batch)
            vectors = embed(list(texts)).tolist()
            collection.upsert(
                ids=list(ids),
                documents=list(texts),
                embeddings=vectors,
                metadatas=[
                    {"content_hash": _content_hash(text), "invoice_id": int(invoice_id)}
                    for invoice_id, text in batch
                ],
            )


def delete(invoice_ids):
    ids = [str(invoice_id) for invoice_id in invoice_ids]
    if not ids:
        return
    with _lock:
        _get_collection().delete(ids=ids)


def query(vector, k=20, allowed_ids=None):
    if k <= 0:
        return []
    with _lock:
        collection = _get_collection()
        count = collection.count()
        if not count or allowed_ids is not None and not allowed_ids:
            return []
        where = None
        if allowed_ids is not None:
            where = {"invoice_id": {"$in": [int(invoice_id) for invoice_id in allowed_ids]}}
        result = collection.query(
            query_embeddings=[vector],
            n_results=min(k, count),
            where=where,
            include=["distances"],
        )
        ids = result["ids"][0]
        distances = result["distances"][0]
        return [(int(invoice_id), 1.0 - float(distance)) for invoice_id, distance in zip(ids, distances)]


def sync(records):
    """Reconcile the persistent index with SQL's current (invoice_id, text) records."""
    records = {str(invoice_id): text for invoice_id, text in records if text}
    with _lock:
        collection = _get_collection()
        existing, offset = {}, 0
        while True:
            batch = collection.get(include=["metadatas"], limit=BATCH_SIZE, offset=offset)
            ids = batch["ids"]
            existing.update(zip(ids, batch["metadatas"]))
            offset += len(ids)
            if len(ids) < BATCH_SIZE:
                break

        stale = set(existing) - set(records)
        if stale:
            collection.delete(ids=sorted(stale))

        changed = []
        for invoice_id, text in records.items():
            if (existing.get(invoice_id) or {}).get("content_hash") != _content_hash(text):
                changed.append((invoice_id, text))

    upsert(changed)
    return {"added_or_updated": len(changed), "deleted": len(stale)}
