import hashlib
import io
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from .config import settings
from .text_utils import chunk_text, normalize_text


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def extract_pdf_pages(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as e:
        raise RuntimeError("pypdf is not installed. Please install requirements.") from e
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: List[Dict[str, Any]] = []
    for i, page in enumerate(reader.pages):
        raw = page.extract_text() or ""
        txt = normalize_text(raw)
        if txt:
            pages.append({"page": i + 1, "text": txt})
    return pages


async def ingest_pdf_to_mongo(
    db,
    filename: str,
    pdf_bytes: bytes,
    embed_fn: Callable[[str], Optional[List[float]]],
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    metadata = metadata or {}
    doc_id = str(uuid4())
    pages = extract_pdf_pages(pdf_bytes)

    chunk_docs = []
    total_chunks = 0

    for page_obj in pages:
        page_no = page_obj["page"]
        text = page_obj["text"]
        chunks = chunk_text(text, settings.chunk_size, settings.chunk_overlap)

        for idx, chunk in enumerate(chunks):
            emb = embed_fn(chunk)
            if emb is None:
                continue
            chunk_docs.append(
                {
                    "doc_id": doc_id,
                    "source": filename,
                    "source_type": "pdf",
                    "page": page_no,
                    "chunk_index": idx,
                    "chunk_hash": _sha1(chunk),
                    "text": chunk,
                    "embedding": emb,
                    "metadata": metadata,
                    "created_at": datetime.utcnow(),
                }
            )
            total_chunks += 1

    if chunk_docs:
        await db[settings.chunks_collection].insert_many(chunk_docs)

    await db[settings.docs_collection].insert_one(
        {
            "doc_id": doc_id,
            "filename": filename,
            "pages": len(pages),
            "chunks": total_chunks,
            "metadata": metadata,
            "created_at": datetime.utcnow(),
        }
    )

    return {
        "doc_id": doc_id,
        "filename": filename,
        "pages_indexed": len(pages),
        "chunks_indexed": total_chunks,
    }


def _extract_image_text(image_bytes: bytes) -> str:
    # Best-effort OCR. Works when pillow+pytesseract are available and tesseract is installed.
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return ""

    try:
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img) or ""
        return normalize_text(text)
    except Exception:
        return ""


async def ingest_image_to_mongo(
    db,
    filename: str,
    image_bytes: bytes,
    embed_fn: Callable[[str], Optional[List[float]]],
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    metadata = metadata or {}
    doc_id = str(uuid4())

    text = _extract_image_text(image_bytes)
    if not text:
        text = f"Image uploaded: {filename}. OCR text unavailable."

    emb = embed_fn(text)
    total_chunks = 0
    if emb is not None:
        await db[settings.chunks_collection].insert_one(
            {
                "doc_id": doc_id,
                "source": filename,
                "source_type": "image",
                "page": 1,
                "chunk_index": 0,
                "chunk_hash": _sha1(text),
                "text": text,
                "embedding": emb,
                "metadata": metadata,
                "created_at": datetime.utcnow(),
            }
        )
        total_chunks = 1

    await db[settings.docs_collection].insert_one(
        {
            "doc_id": doc_id,
            "filename": filename,
            "pages": 1,
            "chunks": total_chunks,
            "metadata": metadata,
            "created_at": datetime.utcnow(),
        }
    )

    return {
        "doc_id": doc_id,
        "filename": filename,
        "pages_indexed": 1,
        "chunks_indexed": total_chunks,
    }

