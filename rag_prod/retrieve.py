import hashlib
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .cache import cache_client
from .config import settings


def _cache_key(
    query: str,
    top_k: int,
    owner_email: Optional[str] = None,
    owner_session_id: Optional[str] = None,
) -> str:
    key_source = owner_session_id or owner_email or 'global'
    h = hashlib.sha1(f"{query}|{top_k}|{key_source}".encode("utf-8")).hexdigest()
    return f"rag:retrieve:{h}"


def _is_generic_document_query(query: str) -> bool:
    lower = (query or "").lower()
    patterns = [
        r"\bthis\s*pdf\b",
        r"\bthispdf\b",
        r"\buploaded\s*pdf\b",
        r"\bthis\s*file\b",
        r"\bthis\s*image\b",
        r"\bthis\s*photo\b",
        r"\bthis\s*picture\b",
        r"\battached\s*file\b",
        r"\battached\s*image\b",
        r"\buploaded\s*file\b",
        r"\buploaded\s*image\b",
        r"\bthe\s*image\b",
        r"\bthe\s*photo\b",
        r"\bthe\s*picture\b",
        r"\bthis\s*document\b",
        r"\bthisdoc\b",
        r"\bthis\s*doc\b",
        r"\bthe\s*document\b",
        r"\bplease\s*summarize\b",
        r"\bdescribe\s*this\b",
        r"\bwhat\s+is\s+this\b",
        r"\btell\s+me\s+(about|something)\b",
        r"\babout\s*(this|the)\b",
        r"\bresume\b",
        r"\bcv\b",
        r"\bdocument\b",
    ]
    return any(re.search(pattern, lower) for pattern in patterns)


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
    owner_session_id: Optional[str] = None,
    force_recent_uploads: bool = False,
    session_only: bool = False,
) -> List[Dict[str, Any]]:
    top_k = top_k or settings.top_k_default
    key = _cache_key(query, top_k, owner_email=owner_email, owner_session_id=owner_session_id)

    skip_cache = force_recent_uploads or (
        (owner_email or owner_session_id) and _is_generic_document_query(query)
    )
    if session_only:
        skip_cache = True
    if not skip_cache:
        cached = cache_client.get_json(key)
        if isinstance(cached, list):
            print(f"✓ RAG cache hit: {len(cached)} results for query '{query[:60]}'")
            return cached

    # Try embedding-based retrieval first
    query_vec = embed_fn(query)
    
    projection = {
        "_id": 0,
        "doc_id": 1,
        "source": 1,
        "source_type": 1,
        "page": 1,
        "chunk_index": 1,
        "text": 1,
        "embedding": 1,
        "created_at": 1,
    }

    if session_only and owner_session_id:
        doc_filter = {"metadata.session_id": owner_session_id}
        print(f"RAG FILTER: session-only metadata.session_id='{owner_session_id}'")
    else:
        filters: List[Dict[str, Any]] = []
        if owner_session_id:
            filters.append({"metadata.session_id": owner_session_id})
        if owner_email:
            filters.append({"metadata.uploaded_by": owner_email})

        if len(filters) > 1:
            doc_filter = {"$or": filters}
            print(f"RAG FILTER: session='{owner_session_id}' OR uploaded_by='{owner_email}'")
        elif len(filters) == 1:
            doc_filter = filters[0]
            print(f"RAG FILTER: {doc_filter}")
        else:
            doc_filter = {}
            print("RAG FILTER: No owner filter (global search)")

    cursor = db[settings.chunks_collection].find(doc_filter, projection).limit(settings.max_candidates)
    rows = await cursor.to_list(length=settings.max_candidates)
    print(f"RAG: Found {len(rows)} candidate chunks (searched {settings.max_candidates} max, filter={doc_filter})")
    
    if not rows:
        print("RAG: No chunks found in collection. Chunks in DB for user? Check /rag/uploaded-docs")
        return []

    def _return_recent_doc_chunks(source_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        doc_chunks = [
            row for row in source_rows
            if row.get("text") and row.get("source_type") in {"pdf", "image", "text", "docx", "pptx", None}
        ]
        if not doc_chunks:
            doc_chunks = [row for row in source_rows if row.get("source") and row.get("text")]
        doc_chunks.sort(
            key=lambda x: (
                x.get("created_at") or datetime.min,
                x.get("page", 0),
                x.get("chunk_index", 0),
            ),
            reverse=True,
        )
        top = doc_chunks[:max(top_k, 12)]
        for row in top:
            row["score"] = 1.0
            row.pop("embedding", None)
        return top

    # Fresh uploads or generic document questions should use the uploaded file text directly.
    if force_recent_uploads or (
        (owner_email or owner_session_id) and _is_generic_document_query(query)
    ):
        reason = "recent upload" if force_recent_uploads else "generic document query"
        print(f"RAG: Returning uploaded file chunks ({reason}).")
        top = _return_recent_doc_chunks(rows)
        cache_client.set_json(key, top)
        return top

    # If query embedding failed, use keyword fallback
    if query_vec is None:
        print("⚠️ RAG: Query embedding failed. Using text keyword fallback.")
        keywords = [w.strip() for w in query.split() if len(w.strip()) > 2]

        scored = []
        for row in rows:
            text_lower = (row.get("text") or "").lower()
            matches = sum(1 for kw in keywords if kw in text_lower)
            if matches > 0:
                row["score"] = matches / len(keywords) if keywords else 0
                scored.append(row)

        scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        top = scored[:top_k]
        if not top:
            print("RAG: No keyword matches; returning recent uploaded chunks as fallback.")
            top = _return_recent_doc_chunks(rows)[:top_k]
        cache_client.set_json(key, top)
        print(f"✓ RAG fallback found {len(top)} results by text matching")
        return top

    # Standard embedding-based retrieval
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
    if scored:
        top = scored[:top_k]
        cache_client.set_json(key, top)
        print(f"✓ RAG retrieved {len(top)} results by semantic similarity")
        return top

    print("⚠️ RAG: No embedded chunks available for semantic search. Falling back to keyword matching.")
    keywords = [w.strip() for w in re.split(r"\s+", query.lower()) if len(w.strip()) > 2]
    scored = []
    for row in rows:
        text_lower = (row.get("text") or "").lower()
        matches = sum(1 for kw in keywords if kw in text_lower)
        if matches > 0:
            row["score"] = matches / len(keywords) if keywords else 0
            row.pop("embedding", None)
            scored.append(row)

    scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    top = scored[:top_k]
    if not top:
        print("RAG: No scored matches; returning recent uploaded chunks as fallback.")
        top = _return_recent_doc_chunks(rows)[:top_k]
    cache_client.set_json(key, top)
    print(f"✓ RAG fallback found {len(top)} results by text matching")
    return top
