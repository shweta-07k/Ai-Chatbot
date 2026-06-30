import os
import re
import sys
import base64
import requests
import json
from datetime import datetime,timedelta
from typing import Optional, List
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from html import unescape
from uuid import uuid4
from fastapi import FastAPI, UploadFile, File, Form, Request
from jose import jwt
from jose import JWTError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.ai.inference import ChatCompletionsClient
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.core.credentials import AzureKeyCredential
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from bson import ObjectId
from contextlib import asynccontextmanager
from fastapi import HTTPException
import bcrypt
from pydantic import BaseModel, EmailStr,Field
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

load_dotenv()

# Windows terminals often default to cp1252; avoid crashing on log emoji.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rag_prod.graph import verify_neo4j_connection

from rag.retriever import retrieve
from rag_prod.ingest import (
    ingest_pdf_to_mongo,
    ingest_image_to_mongo,
    ingest_text_file_to_mongo,
    ingest_docx_to_mongo,
    ingest_pptx_to_mongo,
)
from rag_prod.retrieve import retrieve_chunks_with_cache, _is_generic_document_query
from rag_prod.graph import get_neo4j_driver, graph_search
from rag_prod.config import settings as rag_settings




SECRET_KEY = os.getenv("JWT_SECRET_KEY", "mysecret123")
ALGORITHM = "HS256"
security = HTTPBearer()

# Lazy-load embedder so app can still start if HF/model download is unavailable
embed_model = None
embed_model_failed = False
db_client = AsyncIOMotorClient(
    os.getenv("MONGODB_URI"),
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
)
db = db_client.ai_project

http_session = requests.Session()
http_session.trust_env = False  # Ignore broken proxy env vars that can block live API calls

@asynccontextmanager
async def lifespan(app: FastAPI):
    # This runs right before the server starts accepting HTTP traffic
    await verify_neo4j_connection()
    yield
    # Cleanup tasks can be added here if needed

app = FastAPI(lifespan=lifespan)


def get_github_models_client():
    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if not token or token.startswith("replace_with_"):
        raise HTTPException(
            status_code=503,
            detail=(
                "AI provider is not configured. Add a valid GitHub Models token "
                "to GITHUB_TOKEN in .env, then restart the backend."
            ),
        )

    return ChatCompletionsClient(
        endpoint="https://models.inference.ai.azure.com",
        credential=AzureKeyCredential(token),
    )


def provider_error_reply(error: Exception) -> str:
    if isinstance(error, ClientAuthenticationError):
        return (
            "AI provider rejected the configured credentials. Please create a new "
            "GitHub token with access to GitHub Models, update GITHUB_TOKEN in .env, "
            "and restart the backend."
        )
    if isinstance(error, HttpResponseError) and getattr(error, "status_code", None) in (401, 403):
        return (
            "AI provider authorization failed. Check that GITHUB_TOKEN is valid and "
            "has access to the selected model."
        )
    return f"AI service error: {str(error)}"


def _file_mime_type(filename: str) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def _image_mime_type(filename: str) -> str:
    mime = _file_mime_type(filename)
    return mime if mime.startswith("image/") else "image/jpeg"


def analyze_image_with_vision(image_bytes: bytes, filename: str, user_question: str = "") -> str:
    """Use GPT-4o vision to describe an uploaded image for RAG/chat context."""
    if not image_bytes:
        return ""
    try:
        mime = _image_mime_type(filename)
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        prompt = (
            user_question.strip()
            or "Describe this image in detail. Include visible text, objects, people, colors, layout, and any important context."
        )
        ai_client = get_github_models_client()
        response = ai_client.complete(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                    ],
                }
            ],
            model="gpt-4o",
            max_tokens=700,
            connection_timeout=15,
            read_timeout=45,
        )
        if response and getattr(response, "choices", None):
            content = response.choices[0].message.content
            if content:
                return str(content).strip()
    except Exception as exc:
        print(f"IMAGE VISION ERROR ({filename}): {exc}")
    return ""


def _is_upload_file(value) -> bool:
    return hasattr(value, "read") and hasattr(value, "filename")


def _extract_upload_files(form) -> list:
    """Collect all uploaded files from multipart form (supports repeated 'file' fields)."""
    files = []
    multi_items = getattr(form, "multi_items", None)
    if callable(multi_items):
        for key, value in form.multi_items():
            if key == "file" and _is_upload_file(value):
                files.append(value)
    if files:
        return files

    raw = form.getlist("file")
    files = [item for item in raw if _is_upload_file(item)]
    if files:
        return files

    single = form.get("file")
    if _is_upload_file(single):
        return [single]

    return [value for value in form.values() if _is_upload_file(value)]


async def _load_doc_chunks(db, doc_id: str) -> list:
    from rag_prod.config import settings as rag_cfg

    projection = {
        "_id": 0,
        "doc_id": 1,
        "source": 1,
        "source_type": 1,
        "page": 1,
        "chunk_index": 1,
        "text": 1,
    }
    rows = (
        await db[rag_cfg.chunks_collection]
        .find({"doc_id": doc_id}, projection)
        .sort([("page", 1), ("chunk_index", 1)])
        .to_list(length=500)
    )
    for row in rows:
        row["score"] = 1.0
    return rows


def _chunk_from_text(filename: str, text: str, source_type: str = "text") -> dict:
    return {
        "source": filename,
        "source_type": source_type,
        "page": 1,
        "chunk_index": 0,
        "text": text,
        "score": 1.0,
    }


def _build_fallback_chunks(filename: str, payload: bytes, message: str) -> list:
    lower = (filename or "").lower()
    if lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        vision_text = analyze_image_with_vision(payload, filename, message)
        if vision_text:
            return [_chunk_from_text(filename, vision_text, "image")]
        return []
    if lower.endswith((".txt", ".md", ".log", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm")):
        text = payload.decode("utf-8", errors="ignore").strip()
        if text:
            return [_chunk_from_text(filename, text[:8000], "text")]
    if lower.endswith((".docx", ".doc")):
        try:
            from rag_prod.ingest import extract_text_from_doc_bytes
            text = extract_text_from_doc_bytes(payload, filename)
            if text:
                return [_chunk_from_text(filename, text[:12000], "docx")]
        except Exception as exc:
            print(f"DOCX FALLBACK ERROR: {filename}: {exc}")
    return []


def _friendly_upload_error(filename: str, exc: Exception) -> str:
    ext = os.path.splitext((filename or "").lower())[1]
    msg = str(exc).lower()

    if ext == ".docx" or "docx" in msg or "word document" in msg:
        return (
            f"I couldn't fully read **{filename}** as a Word file. "
            "Please try saving it as **PDF** or **plain text (.txt)** and upload again — that usually works best."
        )
    if ext == ".pptx" or "pptx" in msg or "python-pptx" in msg:
        return (
            f"I couldn't read **{filename}** as a PowerPoint file. "
            "Please export it as **PDF** or upload slides as **images** instead."
        )
    if ext == ".pdf" or "pdf" in msg:
        return (
            f"I had trouble reading **{filename}**. "
            "The PDF might be scanned or protected — try a text-based PDF or paste the content directly."
        )
    if any(ext == e for e in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return (
            f"I couldn't analyze **{filename}** as an image. "
            "Please try a clearer PNG or JPG, or describe what you need in text."
        )

    return (
        f"I couldn't process **{filename}** right now. "
        "Please try **PDF**, **TXT**, or **PNG/JPG** — or paste the important text into the chat."
    )


async def _clear_session_uploads(db, session_id: str) -> dict:
    """Remove prior uploaded file chunks/docs for this chat session only."""
    from rag_prod.config import settings as rag_cfg

    chunk_result = await db[rag_cfg.chunks_collection].delete_many(
        {"metadata.session_id": session_id}
    )
    doc_result = await db[rag_cfg.docs_collection].delete_many(
        {"metadata.session_id": session_id}
    )
    deleted = {
        "chunks_deleted": chunk_result.deleted_count,
        "docs_deleted": doc_result.deleted_count,
    }
    if deleted["chunks_deleted"] or deleted["docs_deleted"]:
        print(f"🧹 Cleared prior session uploads for '{session_id}': {deleted}")
    return deleted


async def _session_has_uploads(db, session_id: str) -> bool:
    from rag_prod.config import settings as rag_cfg

    count = await db[rag_cfg.docs_collection].count_documents(
        {"metadata.session_id": session_id}
    )
    return count > 0


async def _fetch_recent_upload_chunks(
    db,
    *,
    session_id: str,
    filenames: Optional[list] = None,
    limit: int = 20,
) -> list:
    """Return the newest ingested chunks for the current chat session only."""
    from rag_prod.config import settings as rag_cfg

    doc_filter: dict = {"metadata.session_id": session_id}
    if filenames:
        doc_filter = {"$and": [doc_filter, {"source": {"$in": filenames}}]}

    projection = {
        "_id": 0,
        "doc_id": 1,
        "source": 1,
        "source_type": 1,
        "page": 1,
        "chunk_index": 1,
        "text": 1,
        "created_at": 1,
    }
    cursor = (
        db[rag_cfg.chunks_collection]
        .find(doc_filter, projection)
        .sort("created_at", -1)
        .limit(limit)
    )
    rows = await cursor.to_list(length=limit)
    for row in rows:
        row["score"] = 1.0
    return rows


def _merge_chunk_results(primary: list, secondary: list) -> list:
    merged = []
    seen = set()
    for chunk in (primary or []) + (secondary or []):
        key = f"{chunk.get('doc_id')}|{chunk.get('source')}|{chunk.get('page')}|{chunk.get('chunk_index')}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(chunk)
    return merged


def _looks_like_general_question(message: str) -> bool:
    lower = (message or "").lower().strip()
    if not lower:
        return False
    patterns = [
        r"^what (is|are) ",
        r"^who (is|are) ",
        r"^why (is|are|do|does) ",
        r"^how (do|does|can|to|would) ",
        r"^how to ",
        r"^explain ",
        r"^define ",
        r"^tell me about ",
        r"^tell me ",
        r"^difference between ",
        r"^compare ",
        r"^prepare for ",
        r"^can you (help|explain|tell) ",
    ]
    return any(re.search(pattern, lower) for pattern in patterns)


def _is_career_interview_query(message: str) -> bool:
    lower = (message or "").lower()
    patterns = [
        r"interview",
        r"situational question",
        r"prepare for",
        r"how to prepare",
        r"job interview",
        r"hr round",
        r"mock interview",
        r"resume tip",
        r"cover letter",
        r"behavioral question",
    ]
    return any(re.search(pattern, lower) for pattern in patterns)


def _resolve_query_intent(message: str, has_files: bool) -> str:
    """Classify the current user message so the assistant routes to the right brain."""
    if has_files:
        return "upload"
    if is_weather_query(message):
        return "weather"
    if _is_career_interview_query(message):
        return "career"
    if _is_upload_focused_query(message, has_files=False):
        return "upload"
    if needs_live_search(message):
        return "live"
    if _references_prior_context(message):
        return "followup"
    return "general"


def _intent_guidance(intent: str) -> str:
    notes = {
        "general": (
            "Current question type: general knowledge. "
            "Answer only what the user asked in their latest message. "
            "Do not bring in unrelated topics from earlier in the chat."
        ),
        "followup": (
            "Current question type: follow-up. "
            "The user is continuing the previous topic — use recent conversation context."
        ),
        "upload": (
            "Current question type: uploaded document. "
            "Answer from the user's uploaded file content when available."
        ),
        "weather": (
            "Current question type: weather. "
            "Answer about weather using live weather data when provided."
        ),
        "live": (
            "Current question type: live/current information. "
            "Use the real-time search data provided below."
        ),
        "career": (
            "Current question type: interview or career advice. "
            "Give practical, helpful guidance for interviews, jobs, or career preparation."
        ),
    }
    return notes.get(intent, notes["general"])


def _should_include_conversation_history(message: str, intent: str) -> bool:
    """Only keep prior turns when the user is clearly continuing the same topic."""
    if intent == "followup" or _references_prior_context(message):
        return True
    if intent in ("general", "weather", "live", "career"):
        return False
    if intent == "upload" and not _references_prior_context(message):
        return False
    return False


def _looks_like_followup(message: str) -> bool:
    lower = (message or "").lower().strip()
    if not lower:
        return False
    patterns = [
        r"^(tell me more|more details|go on|continue|elaborate)\b",
        r"^explain (that|this|it)( further| more| in detail)?\b",
        r"^(what about|how about)\b",
        r"^(can you (also|explain|clarify|expand|elaborate))\b",
        r"^(and |also )",
        r"^(why is that|why so|what do you mean)\b",
        r"^(ok|okay|thanks|thank you)[,.!?\s]",
    ]
    return any(re.search(pattern, lower) for pattern in patterns)


def _references_prior_context(message: str) -> bool:
    """True when the user is clearly continuing the previous topic."""
    lower = (message or "").lower().strip()
    if _looks_like_followup(message):
        return True
    patterns = [
        r"\b(that|this|it|those|these|above|earlier|previous|before|same|you said|you mentioned|from above)\b",
        r"\b(its|their|his|her)\b",
        r"^(what about|how about)\b",
        r"^(and |also )\b",
    ]
    return any(re.search(pattern, lower) for pattern in patterns)


def _is_upload_focused_query(message: str, has_files: bool = False) -> bool:
    """True when the user wants an answer grounded in uploaded files."""
    if is_weather_query(message) or _is_career_interview_query(message):
        return False
    if _is_generic_document_query(message):
        return True
    lower = (message or "").lower().strip()
    if has_files and "please analyze the attached" in lower:
        return True
    if _looks_like_general_question(message):
        return False
    if _looks_like_followup(message):
        return False
    if has_files and lower and not _looks_like_general_question(message):
        file_terms = ("file", "document", "pdf", "upload", "attached", "resume", "cv", "docx", "doc")
        if any(term in lower for term in file_terms):
            return True
    return False


def _append_chat_history(messages: list, history: list) -> list:
    """Insert prior turns after the system message, before the latest user turn."""
    if not history or not messages:
        return messages
    out = [messages[0]]
    for chat in history:
        user_query = (chat.get("user_query") or "").strip()
        ai_response = (chat.get("ai_response") or "").strip()
        if user_query:
            out.append({"role": "user", "content": user_query})
        if ai_response:
            out.append({"role": "assistant", "content": ai_response})
    out.extend(messages[1:])
    return out


def _model_asks_for_upload(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"please upload (?:a |the |your )?(?:pdf|file|document)",
        r"kindly upload (?:a |the |your )?(?:pdf|file|document)",
        r"you (?:need to|should|must) upload",
        r"could you upload (?:a |the |your )?(?:pdf|file|document)",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def answer_from_upload_context(
    question: str,
    prod_docs: list,
    ai_client,
    raw_files: Optional[list] = None,
    conversation_history: Optional[list] = None,
) -> str:
    raw_files = raw_files or []
    image_files = [
        item for item in raw_files
        if str(item.get("mime", "")).startswith("image/") and item.get("bytes")
    ]

    text_parts = []
    for doc in prod_docs or []:
        text = (doc.get("text") or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if "no readable text could be extracted" in lowered:
            continue
        if "ocr text unavailable" in lowered and len(text) < 120:
            continue
        src = doc.get("source", "unknown")
        page = doc.get("page")
        text_parts.append(f"[source={src}, page={page}]\n{text[:2500]}")

    text_context = "\n\n".join(text_parts)

    if image_files:
        user_content = [
            {
                "type": "text",
                "text": (
                    f"Question:\n{question}\n\n"
                    f"Use the attached image(s) and any extracted text below to answer.\n\n"
                    f"Extracted text context:\n{text_context or 'No extracted text yet.'}"
                ),
            }
        ]
        for item in image_files[:4]:
            encoded = base64.b64encode(item["bytes"]).decode("utf-8")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{item['mime']};base64,{encoded}"},
                }
            )
        messages = [
            {
                "role": "system",
                "content": (
                    "You analyze uploaded files for the user. "
                    "Answer from the attached images and extracted text. "
                    "Use Markdown. Wrap shell/terminal commands in backticks or fenced code blocks. "
                    "Never ask the user to upload again."
                ),
            },
            {"role": "user", "content": user_content},
        ]
    elif text_context:
        messages = [
            {
                "role": "system",
                "content": (
                    "You analyze uploaded files for the user. "
                    "Answer ONLY from the provided file excerpts. "
                    "Summarize, explain, or answer the question directly in helpful Markdown. "
                    "Wrap shell/terminal commands in backticks or fenced code blocks. "
                    "Never ask the user to upload again."
                ),
            },
            {
                "role": "user",
                "content": f"Question:\n{question}\n\nUploaded file excerpts:\n{text_context}",
            },
        ]
    else:
        return (
            "I received your file, but I could not extract readable text from it. "
            "If this is a scanned PDF or image-only file, try uploading a PNG/JPG screenshot or a text-based PDF."
        )

    messages = _append_chat_history(messages, conversation_history or [])

    response = ai_client.complete(
        model="gpt-4o",
        messages=messages,
        max_tokens=1200,
        connection_timeout=15,
        read_timeout=90,
    )
    if not response or not getattr(response, "choices", None):
        return "I processed your upload but could not generate an answer. Please try again."
    return response.choices[0].message.content or "I processed your upload but could not generate an answer."

def get_embed_model():
    global embed_model, embed_model_failed
    if embed_model_failed:
        print("⚠️ EMBED MODEL: Previously failed to load, skipping retry")
        return None
    if embed_model is None:
        try:
            allow_download = os.getenv("ALLOW_MODEL_DOWNLOAD", "").strip() == "1"
            if not allow_download:
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"

            print("🔧 EMBED MODEL: Loading 'all-MiniLM-L6-v2'...")
            from sentence_transformers import SentenceTransformer

            embed_model = SentenceTransformer(
                "all-MiniLM-L6-v2",
                local_files_only=not allow_download,
            )
            print("✓ EMBED MODEL: Loaded successfully")
        except Exception as e:
            embed_model_failed = True
            print(f"❌ EMBED MODEL LOAD ERROR: {e}")
            return None
    return embed_model

def build_embedding(text: str):
    model = get_embed_model()
    if model is None:
        print(f"⚠️ EMBED: Model unavailable, returning None for text '{text[:50]}'")
        return None
    try:
        result = model.encode(text).tolist()
        print(f"✓ EMBED: Successfully encoded {len(text)} chars")
        return result
    except Exception as e:
        print(f"❌ EMBED ERROR: {e}")
        return None

# 2. CORS (explicit dev origins; wildcard + credentials is rejected by browsers)
_cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3001,http://127.0.0.1:3001,"
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
_cors_origin_regex = os.getenv(
    "CORS_ORIGIN_REGEX",
    r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?|https://.*\.onrender\.com",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Real-time data fetching functions
WEATHER_CODE_LABELS = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


LOCATION_ALIASES = {
    "chh sambhajinagar": "Aurangabad",
    "chhatrapati sambhajinagar": "Aurangabad",
    "csn": "Aurangabad",
}


LOCATION_STOP_WORDS = {
    "what", "whats", "is", "the", "weather", "temperature", "forecast",
    "rain", "climate", "in", "at", "for", "near", "today", "now", "current",
    "tell", "me", "todays", "today's", "temp", "upcoming", "details", "and",
    "please", "of", "about", "conditions", "condition", "humidity", "wind",
    "let", "know", "weathe", "wether", "whether", "degree", "degrees",
}


WEATHER_INTENT_TERMS = [
    "weather", "weathe", "wether", "temperature", "temp", "rain", "forecast",
    "climate", "humidity", "wind", "degree", "degrees",
]

LIVE_INFO_TERMS = [
    "current", "today", "now", "latest", "recent", "live", "real time",
    "breaking", "news", "update", "updates", "price", "prices", "stock",
    "crypto", "score", "match", "result", "results", "headlines", "search",
    "who won", "what happened", "this week", "this month", "this year",
]


def normalize_location_query(location: str) -> str:
    cleaned = re.sub(r"[._]+", " ", (location or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return LOCATION_ALIASES.get(cleaned, location.strip())


def compact_location_text(text: str) -> str:
    text = re.sub(r"(?i)\btemp\.\s*in\b", "temp in", text)
    text = re.sub(r"(?i)\bweather\.\s*in\b", "weather in", text)
    text = re.sub(r"[?!)\]]+$", "", text.strip())
    return text


def strip_weather_words(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z\s,.\-']", " ", text)
    tokens = [token.strip(" ,.-'") for token in re.split(r"\s+", cleaned) if token.strip(" ,.-'")]
    kept = [token for token in tokens if token.lower() not in LOCATION_STOP_WORDS]
    return " ".join(kept).strip(" ,.-")


def is_weather_query(message: str) -> bool:
    normalized = compact_location_text(message or "").lower()
    return any(term in normalized for term in WEATHER_INTENT_TERMS)


def needs_live_search(message: str) -> bool:
    normalized = (message or "").lower()
    return any(term in normalized for term in LIVE_INFO_TERMS)


def geocode_query_variants(location: str):
    normalized = normalize_location_query(location)
    cleaned = re.sub(r"\s+", " ", normalized).strip(" ,.-")
    variants = []

    def add(value):
        value = re.sub(r"\s+", " ", value).strip(" ,.-")
        if value and value.lower() not in [v.lower() for v in variants]:
            variants.append(value)

    add(cleaned)
    add(cleaned.split(",", 1)[0])

    words = cleaned.replace(",", " ").split()
    for length in range(len(words) - 1, 0, -1):
        add(" ".join(words[:length]))

    return variants


def score_geo_result(result, requested_location: str):
    requested_tokens = {
        token.lower()
        for token in re.split(r"[\s,.\-']+", requested_location)
        if len(token) > 1
    }
    result_parts = [
        result.get("name", ""),
        result.get("admin1", ""),
        result.get("admin2", ""),
        result.get("country", ""),
        result.get("country_code", ""),
    ]
    result_text = " ".join(str(part).lower() for part in result_parts if part)
    score = 0

    for token in requested_tokens:
        if token in result_text:
            score += 4

    name = str(result.get("name", "")).lower()
    if name and name in requested_location.lower():
        score += 10

    country = str(result.get("country", "")).lower()
    admin1 = str(result.get("admin1", "")).lower()
    if country and country in requested_location.lower():
        score += 8
    if admin1 and admin1 in requested_location.lower():
        score += 6

    population = result.get("population") or 0
    if population:
        score += min(int(population) // 500000, 5)

    return score


def choose_geo_result(results, requested_location: str):
    if not results:
        return None

    requested = normalize_location_query(requested_location).lower()
    preferred_country = "India" if any(
        word in requested
        for word in ["india", "maharashtra", "sambhajinagar", "aurangabad"]
    ) else None

    if preferred_country:
        if "maharashtra" in requested or "sambhajinagar" in requested or "aurangabad" in requested:
            for result in results:
                if result.get("country") == "India" and result.get("admin1") == "Maharashtra":
                    return result

        for result in results:
            if result.get("country") == preferred_country:
                return result

    return max(results, key=lambda result: score_geo_result(result, requested))


def geocode_location(location: str):
    all_results = []
    for variant in geocode_query_variants(location):
        geo_url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={quote_plus(variant)}&count=10&language=en&format=json"
        )
        geo_resp = http_session.get(geo_url, timeout=6)
        geo_resp.raise_for_status()
        results = geo_resp.json().get("results") or []
        if results:
            all_results.extend(results)
            selected = choose_geo_result(all_results, location)
            if selected:
                return selected

    return None


def format_weather_code(code):
    if code is None:
        return "conditions unavailable"
    return WEATHER_CODE_LABELS.get(int(code), f"weather code {code}")


def get_weather(location: str):
    """Fetch current weather and a short forecast; fallback to MET Norway if Open-Meteo is unavailable."""
    try:
        normalized_location = normalize_location_query(location)
        geo_result = geocode_location(normalized_location)
        if not geo_result:
            return f"Could not find location: {location}"

        lat = geo_result["latitude"]
        lon = geo_result["longitude"]
        name_parts = [
            geo_result.get("name"),
            geo_result.get("admin1"),
            geo_result.get("country"),
        ]
        name = ", ".join([part for part in name_parts if part])

        try:
            weather_url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m"
                "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                "&forecast_days=4&timezone=auto&temperature_unit=celsius"
            )
            weather_resp = http_session.get(weather_url, timeout=8)
            weather_resp.raise_for_status()
            weather_data = weather_resp.json()
            current = weather_data.get("current", {})
            temp = current.get("temperature_2m")
            wind = current.get("wind_speed_10m")
            humidity = current.get("relative_humidity_2m")
            condition = format_weather_code(current.get("weather_code"))
            current_time = current.get("time")
            if temp is not None and wind is not None:
                daily = weather_data.get("daily", {})
                dates = daily.get("time", [])
                max_temps = daily.get("temperature_2m_max", [])
                min_temps = daily.get("temperature_2m_min", [])
                rain_chances = daily.get("precipitation_probability_max", [])
                daily_codes = daily.get("weather_code", [])

                forecast_lines = []
                for index, day in enumerate(dates[:4]):
                    high = max_temps[index] if index < len(max_temps) else "N/A"
                    low = min_temps[index] if index < len(min_temps) else "N/A"
                    rain = rain_chances[index] if index < len(rain_chances) else "N/A"
                    day_condition = format_weather_code(daily_codes[index] if index < len(daily_codes) else None)
                    label = "Today" if index == 0 else day
                    forecast_lines.append(
                        f"{label}: {day_condition}, high {high} C, low {low} C, rain chance {rain}%"
                    )

                forecast_text = "\n".join(forecast_lines)
                return (
                    f"Weather in {name}: {temp} C, {condition}, humidity {humidity}%, "
                    f"wind {wind} km/h, as of {current_time} local time.\n"
                    f"Upcoming forecast:\n{forecast_text}\n"
                    "Source: Open-Meteo"
                )
        except Exception as primary_err:
            print(f"OPEN-METEO WEATHER ERROR: {primary_err}")

        met_headers = {"User-Agent": "ai-project-weather/1.0"}
        met_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
        met_resp = None
        last_err = None
        for _ in range(2):
            try:
                met_resp = http_session.get(met_url, headers=met_headers, timeout=8)
                met_resp.raise_for_status()
                break
            except Exception as met_err:
                last_err = met_err
                met_resp = None
        if met_resp is None:
            raise last_err
        met_data = met_resp.json()
        series = met_data.get("properties", {}).get("timeseries", [])
        if not series:
            return f"Weather lookup failed for {name}: no forecast timeseries available."

        first = series[0]
        details = first.get("data", {}).get("instant", {}).get("details", {})
        temp = details.get("air_temperature")
        wind = details.get("wind_speed")
        at_time = first.get("time")
        if temp is None:
            return f"Weather lookup failed for {name}: missing temperature data."

        return (
            f"Weather in {name}: {temp} C, Wind: {wind} m/s, As of: {at_time} "
            "(source: MET Norway Locationforecast)"
        )
    except Exception as e:
        return f"Weather lookup failed: {str(e)}"
def extract_weather_location(message: str):
    """Extract a likely location from weather question text."""
    msg = compact_location_text(message or "")
    if not msg:
        return None

    # Prefer phrases after common prepositions.
    match = re.search(
        r"\b(?:in|at|for|near|of|about)\s+([A-Za-z][A-Za-z\s,.\-']{1,100})",
        msg,
        re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip(" ,.?!")
        # Remove trailing time words often attached to queries.
        candidate = re.sub(
            r"\b(today|tomorrow|now|currently|right now|please|and|upcoming|forecast|details)\b.*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip(" ,.?!")
        if candidate:
            return normalize_location_query(candidate)

    fallback = strip_weather_words(msg)
    if fallback:
        return normalize_location_query(fallback)

    return None

def web_search(query: str):
    """No-key live web/news lookup using free public endpoints."""
    try:
        safe_query = quote_plus(query)

        # 1) Google News RSS fallback (no key required)
        rss_url = f"https://news.google.com/rss/search?q={safe_query}"
        rss_resp = http_session.get(rss_url, timeout=7)
        if rss_resp.ok and rss_resp.text:
            root = ET.fromstring(rss_resp.text)
            items = root.findall(".//item")
            if items:
                news_parts = []
                for item in items[:3]:
                    title = unescape((item.findtext("title") or "Untitled").strip())
                    link = (item.findtext("link") or "").strip()
                    pub = (item.findtext("pubDate") or "").strip()
                    news_parts.append(f"{title} [{pub}] ({link})")
                return "Latest news headlines: " + " | ".join(news_parts)

        # 2) DuckDuckGo instant answer fallback (no key required)
        ddg_url = f"https://api.duckduckgo.com/?q={safe_query}&format=json&no_html=1&no_redirect=1"
        ddg_resp = http_session.get(ddg_url, timeout=7)
        if ddg_resp.ok:
            ddg = ddg_resp.json()
            abstract = (ddg.get("AbstractText") or "").strip()
            abstract_url = (ddg.get("AbstractURL") or "").strip()
            if abstract:
                return f"DuckDuckGo instant result: {abstract} ({abstract_url})"
            related = ddg.get("RelatedTopics") or []
            snippets = []
            for t in related[:3]:
                if isinstance(t, dict) and t.get("Text"):
                    snippets.append(t.get("Text"))
            if snippets:
                return "DuckDuckGo related results: " + " | ".join(snippets)

        return "No recent live results found from available providers."
    except Exception as e:
        return f"Real-time search failed: {str(e)}"

def get_india_equity_market_snapshot():
    """Fetch live India equity index snapshot from NSE + BSE official APIs."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/",
        }

        # NSE (NIFTY 50)
        http_session.get("https://www.nseindia.com", headers=headers, timeout=7)
        nse_resp = http_session.get("https://www.nseindia.com/api/allIndices", headers=headers, timeout=7)
        nse_resp.raise_for_status()
        nse_data = nse_resp.json().get("data", [])
        nifty = next((x for x in nse_data if x.get("indexSymbol") == "NIFTY 50"), None)

        # BSE (SENSEX)
        bse_headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.bseindia.com/",
        }
        bse_resp = http_session.get("https://api.bseindia.com/BseIndiaAPI/api/IndexMovers/w", headers=bse_headers, timeout=7)
        bse_resp.raise_for_status()
        bse_rows = bse_resp.json().get("Table", [])
        sensex = next((x for x in bse_rows if str(x.get("shortalias", "")).upper() == "SENSEX"), None)

        parts = []
        if nifty:
            nifty_as_of = nifty.get("lastUpdateTime") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            parts.append(
                f"NIFTY 50: {nifty.get('last')} (change: {nifty.get('variation')}, {nifty.get('percentChange')}%), "
                f"as_of: {nifty_as_of}, source: NSE"
            )
        if sensex:
            parts.append(
                f"SENSEX: {sensex.get('LTP')} (change: {sensex.get('change')}, {sensex.get('PERCENTCHG')}%), "
                f"as_of: {sensex.get('DT_TM')}, source: BSE"
            )

        if not parts:
            return "No live NSE/BSE index records available."

        return " | ".join(parts)
    except Exception as e:
        return f"Equity snapshot failed: {str(e)}"

def _extract_numbeo_value(html: str, metric_label: str):
    pattern = (
        rf"{re.escape(metric_label)}\s*</td>\s*<td[^>]*>\s*<span[^>]*>"
        r"([^<]+)</span>"
    )
    match = re.search(pattern, html, flags=re.IGNORECASE)
    if not match:
        return None
    value = unescape(match.group(1)).replace("\xa0", " ").strip()
    return value.replace("Ã¢â€šÂ¹", "INR ")

def get_india_home_market_snapshot():
    """Fetch current indicative India home rates from Numbeo + trend headlines."""
    try:
        url = "https://www.numbeo.com/property-investment/country_result.jsp?country=India"
        resp = http_session.get(url, timeout=8)
        resp.raise_for_status()
        html = resp.text

        city_center = _extract_numbeo_value(
            html, "Price per Square Meter to Buy Apartment in City Centre"
        )
        outside_center = _extract_numbeo_value(
            html, "Price per Square Meter to Buy Apartment Outside of Centre"
        )

        if not city_center and not outside_center:
            return "Could not parse India home rates from live source."

        trend = web_search("India housing market latest news today")
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        return (
            f"India residential rate snapshot as_of: {now} | "
            f"City Centre avg: {city_center or 'N/A'} per m^2 | "
            f"Outside Centre avg: {outside_center or 'N/A'} per m^2 | "
            f"source: Numbeo property data page | trend: {trend}"
        )
    except Exception as e:
        return f"Home market snapshot failed: {str(e)}"

def get_current_info():
    """Get current time and date"""
    now = datetime.utcnow()
    return f"Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}"

class ChatRequest(BaseModel):
    message: str
    email: Optional[str] = None
    session_id: Optional[str] = None  # Optional session for grouping conversations


class RagQueryRequest(BaseModel):
    query: str
    top_k: int = rag_settings.top_k_default

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=2)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# create auth checker
def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        email: str = payload.get("email")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")

        return payload

    except JWTError as e:
        print("JWT ERROR:", str(e))
        raise HTTPException(status_code=401, detail="Invalid or expired token")




@app.post("/chat/upload")
async def chat_with_upload(
    message: str = Form(...),
    session_id: str = Form(...),
    email: Optional[str] = Form(default=None),
    file: List[UploadFile] = File(...),
):
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Chat message is required.")
    print(f"📎 /chat/upload received {len(file)} file(s)")
    return await run_chat_message(message, email, session_id, file)


@app.post("/chat")
async def chat_with_ai(request: Request):
    content_type = (request.headers.get("content-type") or "").lower()
    files = []
    if "multipart/form-data" in content_type:
        form = await request.form()
        message = str(form.get("message") or "").strip()
        email = str(form.get("email")) if form.get("email") else None
        session_id = str(form.get("session_id")) if form.get("session_id") else None
        files = _extract_upload_files(form)
        print(f"📎 /chat multipart parsed {len(files)} file(s)")
    else:
        payload = await request.json()
        message = str(payload.get("message") or "").strip()
        email = payload.get("email")
        session_id = payload.get("session_id")
        files = []

    if not message:
        raise HTTPException(status_code=400, detail="Chat message is required.")
    return await run_chat_message(message, email, session_id, files)


async def run_chat_message(
    message: str,
    email: Optional[str],
    session_id: Optional[str],
    files: list,
):
    try:
        if not session_id:
            session_id = f"guest-{uuid4()}"

        user_email = email or f"guest:{session_id}"
        is_guest = email is None
        print(f"📨 CHAT REQUEST: email='{email}', session={session_id}, is_guest={is_guest}, message='{message[:80]}'")

        if files:
            print(f"📦 INGEST ATTACHMENTS: {len(files)} files for session='{session_id}' user='{user_email}'")
            await _clear_session_uploads(db, session_id)
            metadata = {
                "uploaded_by": email or user_email,
                "session_id": session_id,
                "source_label": "chat_attachment",
            }
            just_uploaded_chunks = []
            raw_upload_files = []

            for upload_file in files:
                filename = upload_file.filename or "attachment"
                lower = filename.lower()
                payload = await upload_file.read()
                if not payload:
                    print(f"⚠️ ATTACHMENT SKIPPED: {filename} is empty")
                    continue

                raw_upload_files.append(
                    {
                        "filename": filename,
                        "bytes": payload,
                        "mime": _file_mime_type(filename),
                    }
                )

                vision_fn = None
                if lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    vision_text = analyze_image_with_vision(payload, filename, message)

                    def vision_fn(image_bytes=payload, fname=filename, cached_text=vision_text):
                        return cached_text or analyze_image_with_vision(image_bytes, fname, message)

                try:
                    stats = None
                    if lower.endswith(".pdf"):
                        stats = await ingest_pdf_to_mongo(
                            db=db,
                            filename=filename,
                            pdf_bytes=payload,
                            embed_fn=build_embedding,
                            metadata=metadata,
                        )
                    elif lower.endswith((".docx", ".doc")):
                        stats = await ingest_docx_to_mongo(
                            db=db,
                            filename=filename,
                            docx_bytes=payload,
                            embed_fn=build_embedding,
                            metadata=metadata,
                        )
                    elif lower.endswith(".pptx"):
                        stats = await ingest_pptx_to_mongo(
                            db=db,
                            filename=filename,
                            pptx_bytes=payload,
                            embed_fn=build_embedding,
                            metadata=metadata,
                        )
                    elif lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                        stats = await ingest_image_to_mongo(
                            db=db,
                            filename=filename,
                            image_bytes=payload,
                            embed_fn=build_embedding,
                            metadata=metadata,
                            vision_fn=vision_fn,
                        )
                    else:
                        stats = await ingest_text_file_to_mongo(
                            db=db,
                            filename=filename,
                            file_bytes=payload,
                            embed_fn=build_embedding,
                            metadata=metadata,
                        )

                    if stats and stats.get("doc_id"):
                        doc_chunks = await _load_doc_chunks(db, stats["doc_id"])
                        if doc_chunks:
                            just_uploaded_chunks.extend(doc_chunks)
                            print(f"✓ Loaded {len(doc_chunks)} in-memory chunks for '{filename}'")
                        else:
                            fallback = _build_fallback_chunks(filename, payload, message)
                            just_uploaded_chunks.extend(fallback)
                    else:
                        just_uploaded_chunks.extend(_build_fallback_chunks(filename, payload, message))
                except Exception as exc:
                    print(f"ATTACHMENT INGEST ERROR: {filename}: {exc}")
                    fallback = _build_fallback_chunks(filename, payload, message)
                    if fallback:
                        just_uploaded_chunks.extend(fallback)
                        print(f"✓ Using fallback extracted content for '{filename}'")
                    else:
                        raise HTTPException(
                            status_code=422,
                            detail=_friendly_upload_error(filename, exc),
                        )
        else:
            just_uploaded_chunks = []
            raw_upload_files = []

        recent_history = []
        try:
            query_filter = {"session_id": session_id}
            if not is_guest:
                query_filter["user_email"] = user_email

            recent_history = await db.chat_history.find(query_filter).sort("timestamp", -1).limit(12).to_list(length=12)
            recent_history.reverse()
        except Exception as e:
            print(f"CHAT HISTORY LOAD ERROR: {e}")
        
        # Detect real-time data needs
        real_time_context = ""
        message_lower = message.lower()
        direct_reply = None
        direct_sources = []
        generic_document_query = _is_generic_document_query(message) or bool(files)
        upload_focused_query = _is_upload_focused_query(message, has_files=bool(files))
        query_intent = _resolve_query_intent(message, has_files=bool(files))
        include_history = _should_include_conversation_history(message, query_intent)
        history_for_prompt = recent_history if include_history else []
        print(f"🧠 CHAT INTENT: {query_intent}, upload_focused={upload_focused_query}, include_history={include_history}")
        is_equity_query = any(
            word in message_lower
            for word in ["equity", "stock market", "share market", "nifty", "sensex", "indices", "index"]
        )
        home_terms = ["home", "house", "property", "real estate", "apartment", "flat", "housing"]
        rate_terms = ["rate", "rates", "price", "prices", "market", "cost", "valuation", "situation"]
        is_home_market_query = (
            any(word in message_lower for word in home_terms)
            and any(word in message_lower for word in rate_terms)
        )
        
        if is_equity_query:
            equity_data = get_india_equity_market_snapshot()
            search_data = web_search(message)
            real_time_context = (
                f"\n[{get_current_info()}]"
                f"\n[India Equity Snapshot: {equity_data}]"
                f"\n[Related Live Search: {search_data}]"
            )
            direct_reply = (
                f"As of {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}, India equity snapshot:\n"
                f"{equity_data}\n\n"
                f"Live market context:\n{search_data}"
            )
            direct_sources = ["NSE", "BSE", "Google News RSS/DDG"]

        elif is_home_market_query and any(x in message_lower for x in ["india", "indian"]):
            home_data = get_india_home_market_snapshot()
            search_data = web_search(message)
            real_time_context = (
                f"\n[{get_current_info()}]"
                f"\n[India Home Market Snapshot: {home_data}]"
                f"\n[Related Live Search: {search_data}]"
            )
            direct_reply = (
                f"As of {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}, India home market snapshot:\n"
                f"{home_data}\n\n"
                f"Live market context:\n{search_data}"
            )
            direct_sources = ["Numbeo", "Google News RSS/DDG"]

        elif is_weather_query(message):
            location = extract_weather_location(message)
            if location:
                weather_data = get_weather(location)
                real_time_context = f"\n[Real-time Weather Data: {weather_data}]"
                direct_reply = (
                    f"As of {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}, "
                    f"live weather for {location}:\n{weather_data}\n\n"
                    "Source: Open-Meteo (live API)"
                )
                direct_sources = ["Open-Meteo"]
            else:
                real_time_context = "\n[Location not specified for weather query]"
                direct_reply = "Please specify a city/location for live weather, for example: 'weather in Delhi now'."
                direct_sources = ["Open-Meteo"]
        
        elif needs_live_search(message) and not real_time_context:
            search_data = web_search(message)
            real_time_context = f"\n[{get_current_info()}]\n[Real-time Search Data: {search_data}]"
        
        # Build conversation context
        system_prompt = f"""You are a helpful AI assistant with access to real-time information and optional uploaded document context.

Answer the user's question clearly and accurately.
- You are in a multi-turn chat. Focus on the user's **latest message** and give the answer they expect for that type of question.
- Switch topics naturally: technology questions, weather, interview advice, and uploaded documents are all handled independently unless the user explicitly continues a prior topic.
- For related follow-ups (e.g. "tell me more", "what about its hooks"), use prior conversation context.
- For new unrelated questions, answer only the new question — do not mix in earlier topics.
- For general knowledge questions, answer from your own knowledge in helpful Markdown.
- Wrap shell/terminal commands in inline backticks or fenced ``` code blocks so they are easy to copy.
- Only use uploaded document context when the current question is about uploaded files.
- If real-time context is available below, prioritize it for live data questions.
- If you are unsure, say so clearly.

{_intent_guidance(query_intent)}

{real_time_context}
"""
        messages = [
            {"role": "system", "content": system_prompt}
        ]
        
        # Add previous messages only when the user is continuing the same topic
        for chat in history_for_prompt:
            messages.append({"role": "user", "content": chat.get("user_query", "")})
            messages.append({"role": "assistant", "content": chat.get("ai_response", "")})

        # ===== RAG INTEGRATION START =====
        # Step 1: Embed query only when RAG retrieval is needed (saves ~400MB RAM on Render free tier)
        query_embedding = None
        # Step 2: Retrieve relevant docs only when the question is about uploads
        docs = []
        graph_docs = []
        rag_context = ""
        sources = []
        if upload_focused_query or files or (query_intent == "upload" and generic_document_query):
            query_embedding = build_embedding(message)
            if query_embedding is not None:
                try:
                    docs = retrieve(query_embedding)
                except Exception as e:
                    print("RETRIEVE ERROR:", e)

            try:
                driver = get_neo4j_driver()
                if driver is not None:
                    graph_docs = await graph_search(driver, message, max_results=4)
            except Exception as e:
                print("NEO4J GRAPH SEARCH ERROR:", e)

            if docs:
                rag_context = "\n\nRelevant Information:\n"
                for doc in docs:
                    rag_context += doc.get("text", "") + "\n"
                    sources.append(doc.get("source", "unknown"))

            if graph_docs:
                if not rag_context:
                    rag_context = "\n\nGraphRAG context:\n"
                else:
                    rag_context += "\n\nGraphRAG context:\n"
                for doc in graph_docs:
                    snippet = (doc.get("text") or "")[:700]
                    rag_context += f"- [source={doc.get('source','unknown')}, page={doc.get('page')}] {snippet}\n"
                    sources.append(
                        {
                            "source": doc.get("source", "unknown"),
                            "page": doc.get("page"),
                            "score": doc.get("score"),
                            "doc_id": doc.get("doc_id"),
                            "matched_entities": doc.get("matched_entities"),
                            "source_type": "neo4j_graph",
                        }
                    )

        # Step 4: Add RAG context as an assistant message so the model sees retrieved facts
        prod_context = ""
        # ===== RAG INTEGRATION END =====

        # ===== PRODUCTION RAG (PDF + Mongo + cache) =====
        prod_docs = []
        uploaded_names = [f.filename for f in files if getattr(f, "filename", None)] if files else []
        try:
            if just_uploaded_chunks:
                prod_docs = list(just_uploaded_chunks)
                print(f"📚 RAG: Using {len(prod_docs)} freshly ingested chunks from current upload")
            elif query_intent == "upload" or upload_focused_query or generic_document_query:
                print(f"RAG: Retrieving session uploads for session='{session_id}' user='{user_email}'...")
                prod_docs = await retrieve_chunks_with_cache(
                    db=db,
                    query=message,
                    embed_fn=build_embedding,
                    top_k=12 if generic_document_query else 6,
                    owner_session_id=session_id,
                    session_only=True,
                    force_recent_uploads=generic_document_query,
                )
                if not prod_docs:
                    recent_docs = await _fetch_recent_upload_chunks(
                        db,
                        session_id=session_id,
                        limit=20,
                    )
                    prod_docs = recent_docs
                print(f"📚 RAG: Retrieved {len(prod_docs)} document chunks for user/session")
            else:
                print("CHAT: Skipping upload retrieval for general follow-up question")
        except Exception as e:
            print(f"PROD RAG RETRIEVE ERROR: {e}")
            import traceback
            traceback.print_exc()

        if prod_docs and upload_focused_query:
            prod_context = "\n\nUploaded File Context:\n"
            for d in prod_docs:
                snippet = (d.get("text") or "")[:1800]
                src = d.get("source", "unknown")
                page = d.get("page")
                prod_context += f"- [source={src}, page={page}] {snippet}\n"
                sources.append(
                    {
                        "source": src,
                        "page": page,
                        "score": d.get("score"),
                        "doc_id": d.get("doc_id"),
                    }
                )
        elif prod_docs and not upload_focused_query:
            print("CHAT: General question detected — skipping uploaded file context injection")
            prod_context = ""

        # If any RAG/prod context exists, attach it as an assistant message
        combined_rag = (rag_context or "") + (prod_context or "")
        if combined_rag and upload_focused_query:
            print(f"✓ CHAT: Injecting {len(combined_rag)} chars of RAG context into prompt")
            messages.append({"role": "assistant", "content": f"Retrieved context:\n{combined_rag}"})
            messages.append({
                "role": "assistant",
                "content": (
                    "The user has uploaded documents in this chat. Use the uploaded file context "
                    "to answer when the question is about those files. Do not ask them to upload again."
                ),
            })
        elif combined_rag:
            print("CHAT: RAG context available but skipped for general conversation turn")
        else:
            if not is_guest and query_embedding is not None:
                print(f"RAG: No RAG context available for authenticated user '{user_email}' (check /rag/uploaded-docs)")
            else:
                print(f"RAG: No RAG context available (guest={is_guest}, embedding_ok={query_embedding is not None})")


        messages.append({"role": "user", "content": message})

        # Get AI Response with full context
        if direct_reply:
            ai_reply = direct_reply
            sources.extend([{"source": s, "page": None, "score": None} for s in direct_sources])
        elif files and upload_focused_query:
            ai_client = get_github_models_client()
            try:
                ai_reply = answer_from_upload_context(
                    message,
                    prod_docs,
                    ai_client,
                    raw_files=raw_upload_files,
                    conversation_history=history_for_prompt,
                )
            except (ClientAuthenticationError, HttpResponseError) as e:
                print(f"AI PROVIDER ERROR: {e}")
                return {"reply": provider_error_reply(e), "sources": sources}
        elif prod_docs and await _session_has_uploads(db, session_id) and upload_focused_query:
            ai_client = get_github_models_client()
            try:
                ai_reply = answer_from_upload_context(
                    message,
                    prod_docs,
                    ai_client,
                    conversation_history=history_for_prompt,
                )
            except (ClientAuthenticationError, HttpResponseError) as e:
                print(f"AI PROVIDER ERROR: {e}")
                return {"reply": provider_error_reply(e), "sources": sources}
        else:
            ai_client = get_github_models_client()
            try:
                ai_timeout = 20
                response = ai_client.complete(
                    messages=messages,
                    model="gpt-4o",
                    connection_timeout=15,
                    read_timeout=ai_timeout,
                )
            except (ClientAuthenticationError, HttpResponseError) as e:
                print(f"AI PROVIDER ERROR: {e}")
                return {"reply": provider_error_reply(e), "sources": sources}

            if not response:
                ai_reply = "AI service unavailable."

            elif not hasattr(response, "choices") or len(response.choices) == 0:
                print("EMPTY RESPONSE:", response)
                ai_reply = "No response generated."

            elif not hasattr(response.choices[0], "message"):
                ai_reply = "Invalid response format."

            else:
                ai_reply = response.choices[0].message.content or "Empty response"

            if prod_docs and upload_focused_query and isinstance(ai_reply, str) and _model_asks_for_upload(ai_reply):
                print("CHAT: Model asked for upload despite available context. Retrying with grounded upload answer.")
                try:
                    ai_reply = answer_from_upload_context(
                        message,
                        prod_docs,
                        ai_client,
                        raw_files=raw_upload_files if files else None,
                        conversation_history=history_for_prompt,
                    )
                except Exception as retry_err:
                    print(f"GROUNDED RETRY ERROR: {retry_err}")

        # Save to MongoDB with vector embedding and sources (always persist, including guests)
        vector = query_embedding

        chat_doc = {
             "user_email": user_email,
             "user_query": message,
             "ai_response": ai_reply,
             "embedding": vector or [],
             "session_id": session_id,
             "timestamp": datetime.utcnow(),
             "sources": sources,
             "real_time_context": real_time_context,
             "rag_context": combined_rag,
        }
        try:
            await db.chat_history.insert_one(chat_doc)
        except Exception as e:
            print(f"CHAT HISTORY SAVE ERROR: {e}")

        response_payload = {"reply": ai_reply, "sources": sources}
        if session_id:
            response_payload["session_id"] = session_id

        return response_payload
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error: {e}")
        return {"reply": f"Infrastructure Error: {str(e)}"}

# 5. NEW ENDPOINT: Fetch history on React Refresh
@app.get("/history")
async def get_history(user=Depends(get_current_user)):
    email = user["email"]

    cursor = db.chat_history.find({"user_email": email})
    messages = await cursor.to_list(length=20)

    for m in messages:
        m["_id"] = str(m["_id"])

    return messages


@app.get("/history/session/{session_id}")
async def get_session_history(session_id: str):
    """Return messages for a given session_id (useful for guest/unauthenticated frontend)."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    cursor = db.chat_history.find({"session_id": session_id}).sort("timestamp", -1)
    messages = await cursor.to_list(length=100)

    for m in messages:
        m["_id"] = str(m["_id"])

    return messages



@app.delete("/delete-history/{msg_id}")
async def delete_single_history(
    msg_id: str,
    user=Depends(get_current_user)   # Ã¢Å“â€¦ JWT PROTECTION
):
    if not ObjectId.is_valid(msg_id):
        raise HTTPException(status_code=400, detail="Invalid ObjectId")

    email = user["email"]  # Ã¢Å“â€¦ Get user from token

    result = await db.chat_history.delete_one({
        "_id": ObjectId(msg_id),
        "user_email": email   # Ã¢Å“â€¦ CRITICAL SECURITY CHECK
    })

    if result.deleted_count == 1:
        return {"status": "success"}

    return {"status": "error", "message": "Not found or unauthorized"}


@app.get("/search-memory")
async def search_memory(query: str, user=Depends(get_current_user)):
    email = user["email"]
    
    try:
        # Try semantic vector search first
        query_vector = build_embedding(query)

        results = []
        if query_vector is not None:
            pipeline = [
                {
                    "$vectorSearch": {
                        "index": "vector_index",
                        "path": "embedding",
                        "queryVector": query_vector,
                        "numCandidates": 50,
                        "limit": 5,
                        "filter": {
                            "user_email": email
                        }
                    }
                }
            ]
            results = await db.chat_history.aggregate(pipeline).to_list(length=5)

        if not results:
            # Fallback: one-word / keyword matches using case-insensitive regex
            terms = [t for t in re.split(r"\s+", query.strip()) if t]
            if terms:
                regex = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
                fallback_cursor = db.chat_history.find({
                    "user_email": email,
                    "$or": [
                        {"user_query": regex},
                        {"ai_response": regex}
                    ]
                }).sort("timestamp", -1).limit(5)
                results = await fallback_cursor.to_list(length=5)

        # Convert ObjectId to string for returned documents
        for r in results:
            r["_id"] = str(r["_id"])

        return results

    except Exception as e:
        print("SEARCH ERROR:", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """Fast root probe for Render deploy / load balancer checks."""
    return {"service": "nova-ai-api", "status": "ok"}


@app.get("/ping")
async def ping():
    return {"ok": True}


@app.get("/health")
async def health_check():
    """Fast health check for Render deploy probes (must respond in <1s)."""
    return {"status": "ok"}


@app.get("/health/status")
async def health_status():
    """Detailed stack health check for frontend/devops."""
    mongo_ok = False
    mongo_error = None
    try:
        await db.command("ping")
        mongo_ok = True
    except Exception as exc:
        mongo_error = str(exc)

    github_token_configured = bool((os.getenv("GITHUB_TOKEN") or "").strip()) and not (
        os.getenv("GITHUB_TOKEN") or ""
    ).strip().startswith("replace_with_")

    github_token_ok = None
    github_token_error = None
    if github_token_configured:
        try:
            client = get_github_models_client()
            response = client.complete(
                messages=[{"role": "user", "content": "Reply with OK"}],
                model="gpt-4o-mini",
                max_tokens=3,
                connection_timeout=10,
                read_timeout=15,
            )
            github_token_ok = bool(response and getattr(response, "choices", None))
        except ClientAuthenticationError:
            github_token_ok = False
            github_token_error = "Authentication failed. Create a new GitHub Models token and update GITHUB_TOKEN."
        except HttpResponseError as exc:
            github_token_ok = False
            github_token_error = f"Provider HTTP error ({getattr(exc, 'status_code', 'unknown')}): {exc.message if hasattr(exc, 'message') else str(exc)}"
        except HTTPException as exc:
            github_token_ok = False
            github_token_error = exc.detail
        except Exception as exc:
            github_token_ok = False
            github_token_error = str(exc)

    neo4j_ok = False
    neo4j_error = None
    try:
        driver = get_neo4j_driver()
        if driver is None:
            neo4j_error = "Neo4j disabled or not configured"
        else:
            async with driver.session() as session:
                result = await session.run("RETURN 1 AS v")
                record = await result.single()
                neo4j_ok = record is not None and record.get("v") == 1
    except Exception as exc:
        neo4j_error = str(exc)

    return {
        "status": "ok" if mongo_ok else "degraded",
        "mongodb": {"ok": mongo_ok, "error": mongo_error},
        "github_token": {
            "configured": github_token_configured,
            "ok": github_token_ok,
            "error": github_token_error,
        },
        "neo4j": {"ok": neo4j_ok, "error": neo4j_error},
        "embed_model": {"loaded": embed_model is not None, "failed": embed_model_failed},
    }


@app.get("/rag/uploaded-docs")
async def list_uploaded_docs(user=Depends(get_current_user)):
    """List all documents uploaded by the current user (for debugging)."""
    try:
        email = user.get("email")
        print(f"🔍 DEBUG: Listing docs for user '{email}'")
        
        # Try to find docs with this email
        docs = await db[rag_settings.docs_collection].find(
            {"metadata.uploaded_by": email}
        ).to_list(length=100)
        
        print(f"  Found {len(docs)} docs matching metadata.uploaded_by='{email}'")
        
        # Also count chunks
        chunk_count = await db[rag_settings.chunks_collection].count_documents(
            {"metadata.uploaded_by": email}
        )
        print(f"  Found {chunk_count} chunks matching metadata.uploaded_by='{email}'")
        
        # Get ALL documents to show what's stored
        all_docs = await db[rag_settings.docs_collection].find({}).to_list(length=100)
        print(f"  Total documents in DB: {len(all_docs)}")
        
        # Get sample of first chunk to see structure
        sample_chunk = await db[rag_settings.chunks_collection].find_one({})
        if sample_chunk:
            print(f"  Sample chunk keys: {list(sample_chunk.keys())}")
            print(f"  Sample metadata: {sample_chunk.get('metadata', 'NO METADATA')}")
        
        return {
            "user_email": email,
            "user_documents": [{"filename": d.get("filename"), "chunks": d.get("chunks")} for d in docs],
            "user_chunk_count": chunk_count,
            "total_documents_in_db": len(all_docs),
            "sample_chunk_structure": "Check server logs for details"
        }
    except Exception as e:
        print(f"DEBUG DOCS ERROR: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/rag/session/{session_id}")
async def clear_session_uploads(session_id: str, user=Depends(get_current_user)):
    """Delete uploaded file data tied to a chat session for the logged-in user."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    email = user.get("email")
    from rag_prod.config import settings as rag_cfg

    owned = await db[rag_cfg.docs_collection].count_documents(
        {
            "metadata.session_id": session_id,
            "metadata.uploaded_by": email,
        }
    )
    if owned == 0:
        owned = await db[rag_cfg.docs_collection].count_documents(
            {"metadata.session_id": session_id}
        )
        if owned == 0:
            return {"status": "ok", "deleted": {"chunks_deleted": 0, "docs_deleted": 0}}

    deleted = await _clear_session_uploads(db, session_id)
    return {"status": "ok", "deleted": deleted}


@app.delete("/rag/my-uploads")
async def clear_my_legacy_uploads(user=Depends(get_current_user)):
    """Delete all stored upload chunks/docs for the current user (fresh start)."""
    email = user.get("email")
    from rag_prod.config import settings as rag_cfg

    chunk_result = await db[rag_cfg.chunks_collection].delete_many(
        {"metadata.uploaded_by": email}
    )
    doc_result = await db[rag_cfg.docs_collection].delete_many(
        {"metadata.uploaded_by": email}
    )
    return {
        "status": "ok",
        "deleted": {
            "chunks_deleted": chunk_result.deleted_count,
            "docs_deleted": doc_result.deleted_count,
        },
    }


@app.get("/neo4j/health")
async def neo4j_health():
    """Lightweight health check for Neo4j connectivity.

    Returns JSON indicating whether Neo4j driver was configured and a simple test query result.
    """
    try:
        driver = get_neo4j_driver()
        if driver is None:
            return {"neo4j": False, "detail": "NEO4J_URI/credentials not configured"}

        async with driver.session() as session:
            result = await session.run("RETURN 1 AS v")
            record = await result.single()
            value = record.get("v") if record is not None else None

        return {"neo4j": True, "value": value}
    except Exception as e:
        return {"neo4j": False, "error": str(e)}


def _multipart_installed() -> bool:
    try:
        import multipart  # type: ignore # noqa: F401
        return True
    except Exception:
        return False


if _multipart_installed():
    @app.post("/rag/ingest-file")
    async def rag_ingest_file(
        file: UploadFile = File(...),
        source_label: str = Form(default=""),
        user=Depends(get_current_user),
    ):
        try:
            filename = file.filename or "uploaded_file"
            lower = filename.lower()
            allowed_text = (
                ".txt", ".md", ".log", ".json", ".yaml", ".yml",
                ".csv", ".xml", ".html", ".htm",
            )
            allowed_binary = (".pdf", ".png", ".jpg", ".jpeg", ".docx", ".pptx")
            if not lower.endswith(allowed_text + allowed_binary):
                raise HTTPException(
                    status_code=400,
                    detail="Supported files: pdf, png, jpg, jpeg, txt, md, log, json, yaml, yml, csv, xml, html, htm, docx, pptx",
                )

            payload = await file.read()
            if not payload:
                raise HTTPException(status_code=400, detail="Empty file.")

            print(f"📤 INGEST: Starting '{filename}' ({len(payload)} bytes) for user '{user.get('email')}'...")

            metadata = {
                "uploaded_by": user.get("email"),
                "source_label": source_label.strip() if source_label else "",
            }

            if lower.endswith(".pdf"):
                stats = await ingest_pdf_to_mongo(
                    db=db,
                    filename=filename,
                    pdf_bytes=payload,
                    embed_fn=build_embedding,
                    metadata=metadata,
                )
            elif lower.endswith(".docx"):
                from rag_prod.ingest import ingest_docx_to_mongo

                stats = await ingest_docx_to_mongo(
                    db=db,
                    filename=filename,
                    docx_bytes=payload,
                    embed_fn=build_embedding,
                    metadata=metadata,
                )
            elif lower.endswith(".pptx"):
                from rag_prod.ingest import ingest_pptx_to_mongo

                stats = await ingest_pptx_to_mongo(
                    db=db,
                    filename=filename,
                    pptx_bytes=payload,
                    embed_fn=build_embedding,
                    metadata=metadata,
                )
            elif lower.endswith(allowed_text):
                from rag_prod.ingest import ingest_text_file_to_mongo

                stats = await ingest_text_file_to_mongo(
                    db=db,
                    filename=filename,
                    file_bytes=payload,
                    embed_fn=build_embedding,
                    metadata=metadata,
                )
            elif lower.endswith(('.png', '.jpg', '.jpeg')):
                stats = await ingest_image_to_mongo(
                    db=db,
                    filename=filename,
                    image_bytes=payload,
                    embed_fn=build_embedding,
                    metadata=metadata,
                )
            else:
                stats = await ingest_text_file_to_mongo(
                    db=db,
                    filename=filename,
                    file_bytes=payload,
                    embed_fn=build_embedding,
                    metadata=metadata,
                )

            print(f"✓ INGEST: Completed '{filename}' - {stats}")
            return {"status": "success", "ingestion": stats}
        except HTTPException:
            raise
        except Exception as e:
            print(f"❌ RAG INGEST FILE ERROR: {e}")
            raise HTTPException(status_code=500, detail=f"RAG ingestion failed: {str(e)}")

    @app.post("/rag/ingest-pdf")
    async def rag_ingest_pdf(
        file: UploadFile = File(...),
        source_label: str = Form(default=""),
        user=Depends(get_current_user),
    ):
        try:
            filename = file.filename or "uploaded.pdf"
            if not filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=400, detail="Only PDF files are supported.")

            payload = await file.read()
            if not payload:
                raise HTTPException(status_code=400, detail="Empty file.")

            metadata = {
                "uploaded_by": user.get("email"),
                "source_label": source_label.strip() if source_label else "",
            }

            stats = await ingest_pdf_to_mongo(
                db=db,
                filename=filename,
                pdf_bytes=payload,
                embed_fn=build_embedding,
                metadata=metadata,
            )
            return {"status": "success", "ingestion": stats}
        except HTTPException:
            raise
        except Exception as e:
            print("RAG INGEST ERROR:", e)
            raise HTTPException(status_code=500, detail=f"RAG ingestion failed: {str(e)}")
else:
    @app.post("/rag/ingest-file")
    async def rag_ingest_file(user=Depends(get_current_user)):
        raise HTTPException(
            status_code=503,
            detail="File upload requires python-multipart. Install dependencies from requirements.txt.",
        )

    @app.post("/rag/ingest-pdf")
    async def rag_ingest_pdf(user=Depends(get_current_user)):
        raise HTTPException(
            status_code=503,
            detail="PDF upload requires python-multipart. Install dependencies from requirements.txt.",
        )


@app.post("/rag/query")
async def rag_query(payload: RagQueryRequest, user=Depends(get_current_user)):
    try:
        query = payload.query.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Query cannot be empty.")

        top_k = max(1, min(payload.top_k, 10))
        chunks = await retrieve_chunks_with_cache(
            db=db,
            query=query,
            embed_fn=build_embedding,
            top_k=top_k,
            owner_email=user.get("email"),
        )

        if not chunks:
            chunks = []

        graph_chunks = []
        try:
            driver = get_neo4j_driver()
            if driver is not None:
                graph_chunks = await graph_search(driver, query, max_results=top_k)
        except Exception as e:
            print("RAG QUERY GRAPH SEARCH ERROR:", e)

        if graph_chunks:
            existing_keys = {
                f"{c.get('doc_id')}|{c.get('source')}|{c.get('page')}|{c.get('chunk_index')}"
                for c in chunks
            }
            for item in graph_chunks:
                key = f"{item.get('doc_id')}|{item.get('source')}|{item.get('page')}|{item.get('chunk_index')}"
                if key not in existing_keys:
                    chunks.append(item)
                    existing_keys.add(key)

        if not chunks:
            return {
                "answer": "I could not find relevant content in uploaded files yet.",
                "sources": [],
                "cached": True,
            }

        context = "\n".join(
            [
                f"[source={c.get('source')}, page={c.get('page')}, score={c.get('score')}] {c.get('text', '')[:700]}"
                for c in chunks
            ]
        )

        ai_client = get_github_models_client()
        prompt = (
            "Answer the user only with facts supported by the context. "
            "If information is missing, say so clearly. Include a short 'Sources' list."
        )
        try:
            response = ai_client.complete(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Question: {query}\n\nContext:\n{context}"},
                ],
                connection_timeout=10,
                read_timeout=20,
            )
        except (ClientAuthenticationError, HttpResponseError) as e:
            print(f"RAG AI PROVIDER ERROR: {e}")
            raise HTTPException(status_code=503, detail=provider_error_reply(e))
        if not response or not getattr(response, "choices", None):
            answer = "RAG answer generation failed."
        else:
            answer = response.choices[0].message.content or "RAG answer generation failed."

        sources = [
            {
                "source": c.get("source"),
                "page": c.get("page"),
                "score": c.get("score"),
                "doc_id": c.get("doc_id"),
            }
            for c in chunks
        ]
        return {"answer": answer, "sources": sources, "cached": False}
    except HTTPException:
        raise
    except Exception as e:
        print("RAG QUERY ERROR:", e)
        raise HTTPException(status_code=500, detail=f"RAG query failed: {str(e)}")


# Helper function to hash password
def get_password_hash(password: str):
    pwd_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')

class UserRegister(BaseModel):
    username: str
    email: str # Use str first to test, or EmailStr if you have the library
    password: str = Field(..., min_length=8)
    
@app.post("/register")
async def register_user(user: UserRegister):
    try:
        # 1. Check if email already exists in MongoDB
        existing_user = await db["users"].find_one({"email": user.email})
        if existing_user:
            raise HTTPException(status_code=400, detail="An account with this email already exists. Please log in instead.")

        # 2. Hash the password before saving
        hashed_password = get_password_hash(user.password)

        new_user = {
            "username": user.username,
            "email": user.email,
            "password": hashed_password  # Store ONLY the hash
        }

        await db["users"].insert_one(new_user)
        token = create_access_token({"email": user.email})
        return {
            "access_token": token,
            "status": "success",
            "username": user.username,
            "email": user.email
        }
    except HTTPException:
        raise
    except Exception as e:
        print("REGISTER DB ERROR:", e)
        raise HTTPException(
            status_code=503,
            detail="Database connection failed. Check MONGODB_URI/network and try again."
        )

class UserLogin(BaseModel):
    email: str
    password: str


class GoogleAuthRequest(BaseModel):
    credential: Optional[str] = None
    access_token: Optional[str] = None


async def _google_profile_from_access_token(access_token: str) -> dict:
    try:
        resp = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        print("GOOGLE USERINFO ERROR:", exc)
        raise HTTPException(status_code=503, detail="Could not verify Google sign-in. Please try again.")

    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Google sign-in failed. Please try again.")

    data = resp.json()
    email = data.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google did not provide an email address.")
    return {
        "email": email,
        "username": data.get("name") or email.split("@")[0],
        "avatar_url": data.get("picture"),
        "google_id": data.get("sub"),
        "email_verified": data.get("email_verified", True),
    }


@app.get("/config/public")
async def public_config():
    google_client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("REACT_APP_GOOGLE_CLIENT_ID") or ""
    return {"google_client_id": google_client_id}


@app.post("/auth/google")
async def google_auth(payload: GoogleAuthRequest):
    google_client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("REACT_APP_GOOGLE_CLIENT_ID")
    if not google_client_id:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured yet. Please use email and password.",
        )

    if payload.access_token:
        profile = await _google_profile_from_access_token(payload.access_token)
        email = profile["email"]
        username = profile["username"]
        avatar_url = profile.get("avatar_url")
        google_id = profile.get("google_id")
    elif payload.credential:
        try:
            token_resp = requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": payload.credential},
                timeout=10,
            )
        except requests.RequestException as exc:
            print("GOOGLE TOKEN VERIFY ERROR:", exc)
            raise HTTPException(status_code=503, detail="Could not verify Google sign-in. Please try again.")

        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Google sign-in failed. Please try again.")

        token_data = token_resp.json()
        if token_data.get("aud") != google_client_id:
            raise HTTPException(status_code=400, detail="Google sign-in could not be verified for this app.")

        email = token_data.get("email")
        if not email:
            raise HTTPException(status_code=400, detail="Google did not provide an email address.")

        if token_data.get("email_verified") not in (True, "true", "True", "1", 1):
            raise HTTPException(status_code=400, detail="Please verify your Google email before signing in.")

        username = token_data.get("name") or email.split("@")[0]
        avatar_url = token_data.get("picture")
        google_id = token_data.get("sub")
    else:
        raise HTTPException(status_code=400, detail="Google sign-in token missing. Please try again.")

    existing_user = await db["users"].find_one({"email": email})
    if not existing_user:
        new_user = {
            "username": username,
            "email": email,
            "password": None,
            "auth_provider": "google",
            "google_id": google_id,
            "avatar_url": avatar_url,
            "created_at": datetime.utcnow(),
        }
        await db["users"].insert_one(new_user)
    else:
        patch = {"auth_provider": existing_user.get("auth_provider") or "google"}
        if avatar_url:
            patch["avatar_url"] = avatar_url
        if google_id:
            patch["google_id"] = google_id
        if not existing_user.get("username"):
            patch["username"] = username
        await db["users"].update_one({"email": email}, {"$set": patch})
        existing_user = await db["users"].find_one({"email": email})
        username = existing_user.get("username") or username

    token = create_access_token({"email": email})
    return {
        "access_token": token,
        "status": "success",
        "username": username,
        "email": email,
        "auth_provider": "google",
        "avatar_url": avatar_url,
    }


class ProfileUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None


def _serialize_user(doc: dict) -> dict:
    if not doc:
        return {}
    return {
        "username": doc.get("username") or "",
        "email": doc.get("email") or "",
        "auth_provider": doc.get("auth_provider") or "email",
        "avatar_url": doc.get("avatar_url"),
        "created_at": doc.get("created_at"),
    }


@app.get("/me")
async def get_profile(user=Depends(get_current_user)):
    email = user.get("email")
    existing = await db["users"].find_one({"email": email})
    if not existing:
        raise HTTPException(status_code=404, detail="User profile not found.")
    return _serialize_user(existing)


@app.put("/me")
async def update_profile(payload: ProfileUpdate, user=Depends(get_current_user)):
    email = user.get("email")
    existing = await db["users"].find_one({"email": email})
    if not existing:
        raise HTTPException(status_code=404, detail="User profile not found.")

    updates: dict = {}
    if payload.username is not None:
        username = payload.username.strip()
        if len(username) < 2:
            raise HTTPException(status_code=400, detail="Display name must be at least 2 characters.")
        updates["username"] = username

    if payload.password:
        if len(payload.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
        updates["password"] = get_password_hash(payload.password)

    if not updates:
        raise HTTPException(status_code=400, detail="No profile changes were provided.")

    await db["users"].update_one({"email": email}, {"$set": updates})
    refreshed = await db["users"].find_one({"email": email})
    return {"status": "success", "user": _serialize_user(refreshed)}


@app.post("/login-disabled")
async def login_user(user: UserLogin):
    existing_user = await db["users"].find_one({"email": user.email})

    if not existing_user:
        raise HTTPException(status_code=400, detail="User not found")

    if not bcrypt.checkpw(
        user.password.encode("utf-8"),
        existing_user["password"].encode("utf-8")
    ):
        raise HTTPException(status_code=400, detail="Invalid password")

    # Ã¢Å“â€¦ create token AFTER validation
    token = create_access_token({"email": user.email})

    return {
        "access_token": token,
        "status": "success",
        "username": existing_user["username"],
        "email": existing_user["email"]
    }


@app.post("/login")
async def login_user_safe(user: UserLogin):
    try:
        existing_user = await db["users"].find_one({"email": user.email})

        if not existing_user:
            raise HTTPException(status_code=400, detail="We couldn't find an account with that email. Please register first.")

        stored_password = existing_user.get("password")
        if not stored_password:
            raise HTTPException(
                status_code=400,
                detail="This account uses Google sign-in. Please continue with Google instead.",
            )

        if not bcrypt.checkpw(
            user.password.encode("utf-8"),
            stored_password.encode("utf-8")
        ):
            raise HTTPException(status_code=400, detail="That password doesn't look right. Please try again.")

        token = create_access_token({"email": user.email})

        return {
            "access_token": token,
            "status": "success",
            "username": existing_user["username"],
            "email": existing_user["email"]
        }
    except HTTPException:
        raise
    except Exception as e:
        print("LOGIN DB ERROR:", e)
        raise HTTPException(
            status_code=503,
            detail="Database connection failed. Check MONGODB_URI/network and try again."
        )

