import hashlib
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .cache import cache_client
from .config import settings


def _cache_key(query: str, top_k: int, owner_email: Optional[str] = None) -> str:
    h = hashlib.sha1(f"{query}|{top_k}|{owner_email or 'global'}".encode("utf-8")).hexdigest()
    return f"rag:retrieve:{h}"


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


async def retrieve_chunks_with_cache(
    db,
    query: str,
    embed_fn: Callable[[str], Optional[List[float]]],
    top_k: Optional[int] = None,
    owner_email: Optional[str] = None,
) -> List[Dict[str, Any]]:
    top_k = top_k or settings.top_k_default
    key = _cache_key(query, top_k, owner_email=owner_email)

    cached = cache_client.get_json(key)
    if isinstance(cached, list):
        return cached

    query_vec = embed_fn(query)
    if query_vec is None:
        return []

    projection = {
        "_id": 0,
        "doc_id": 1,
        "source": 1,
        "source_type": 1,
        "page": 1,
        "chunk_index": 1,
        "text": 1,
        "embedding": 1,
    }
    doc_filter: Dict[str, Any] = {}
    if owner_email:
        doc_filter["metadata.uploaded_by"] = owner_email

    cursor = db[settings.chunks_collection].find(doc_filter, projection).limit(settings.max_candidates)
    rows = await cursor.to_list(length=settings.max_candidates)
    if not rows:
        return []

    q = np.asarray(query_vec, dtype=np.float32)
    scored = []

    for row in rows:
        emb = row.get("embedding")
        if not emb:
            continue
        v = np.asarray(emb, dtype=np.float32)
        score = _cosine_sim(q, v)
        row["score"] = round(score, 6)
        row.pop("embedding", None)
        scored.append(row)

    scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    top = scored[:top_k]

    cache_client.set_json(key, top)
    return top
