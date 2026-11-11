"""Embedding backends for Chroma. Default sentence-transformers embeddings run locally."""
import threading
import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer
from .config import settings

_w = HashingVectorizer(n_features=1024, analyzer="word", ngram_range=(1, 2), alternate_sign=False, norm="l2", lowercase=True)
_c = HashingVectorizer(n_features=1024, analyzer="char_wb", ngram_range=(3, 5), alternate_sign=False, norm="l2", lowercase=True)
_st = None
_st_lock = threading.Lock()


def embed(texts):
    global _st
    if settings.embedding_backend == "st":
        if _st is None:
            with _st_lock:
                if _st is None:
                    from sentence_transformers import SentenceTransformer
                    _st = SentenceTransformer(settings.embedding_model)
        return np.asarray(_st.encode(texts, normalize_embeddings=True), dtype=np.float32)
    if settings.embedding_backend != "hash":
        raise ValueError(f"Unsupported EMBEDDING_BACKEND '{settings.embedding_backend}'. Use 'st' or 'hash'.")
    m = np.hstack([_w.transform(texts).toarray(), 0.7 * _c.transform(texts).toarray()]).astype(np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True); n[n == 0] = 1
    return m / n
