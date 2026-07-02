import os
import re
import sys
import asyncio
import base64
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timedelta
from typing import Optional, List
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from html import unescape
from uuid import uuid4
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse
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




_DEFAULT_JWT_SECRET = "mysecret123"
SECRET_KEY = (os.getenv("JWT_SECRET_KEY") or _DEFAULT_JWT_SECRET).strip()
if os.getenv("RENDER") and SECRET_KEY == _DEFAULT_JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET_KEY must be set in Render environment variables (not the dev default)."
    )
if SECRET_KEY == _DEFAULT_JWT_SECRET:
    print("WARNING: Using default JWT_SECRET_KEY. Add JWT_SECRET_KEY to .env for auth.")
ALGORITHM = "HS256"
security = HTTPBearer()

ADMIN_EMAILS = {
    e.strip().lower()
    for e in (os.getenv("ADMIN_EMAILS") or "shweta@gmail.com").split(",")
    if e.strip()
}
ADMIN_SEED_EMAIL = "shweta@gmail.com"
ADMIN_SEED_PASSWORD = "Shweta@321"


def _google_client_id() -> str:
    return (os.getenv("GOOGLE_CLIENT_ID") or os.getenv("REACT_APP_GOOGLE_CLIENT_ID") or "").strip()


def _is_admin_email(email: str) -> bool:
    return (email or "").strip().lower() in ADMIN_EMAILS


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _email_lookup_filter(email: str) -> dict:
    normalized = _normalize_email(email)
    if not normalized:
        return {}
    return {"email": {"$regex": f"^{re.escape(normalized)}$", "$options": "i"}}


def _object_id_timestamp(doc: dict) -> datetime:
    oid = doc.get("_id")
    if isinstance(oid, ObjectId):
        return oid.generation_time.replace(tzinfo=None)
    return datetime.utcnow()

# Lazy-load embedder so app can still start if HF/model download is unavailable
embed_model = None
embed_model_failed = False
db_client = AsyncIOMotorClient(
    os.getenv("MONGODB_URI"),
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
)
MONGODB_DB_NAME = (os.getenv("MONGODB_DB_NAME") or "ai_project").strip()
db = db_client[MONGODB_DB_NAME]

http_session = requests.Session()
http_session.trust_env = False  # Ignore broken proxy env vars that can block live API calls


def get_password_hash(password: str):
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


async def ensure_admin_user():
    email = ADMIN_SEED_EMAIL.lower()
    hashed = get_password_hash(ADMIN_SEED_PASSWORD)
    existing = await db["users"].find_one({"email": email})
    if not existing:
        await db["users"].insert_one({
            "username": "Shweta",
            "email": email,
            "password": hashed,
            "auth_provider": "email",
            "is_admin": True,
            "created_at": datetime.utcnow(),
        })
        print(f"Admin account seeded: {email}")
        return
    updates = {"is_admin": True, "password": hashed}
    if not existing.get("username"):
        updates["username"] = "Shweta"
    await db["users"].update_one({"email": email}, {"$set": updates})


@asynccontextmanager
async def lifespan(app: FastAPI):
    # This runs right before the server starts accepting HTTP traffic
    await verify_neo4j_connection()
    try:
        await ensure_admin_user()
        await _ensure_admin_indexes()
        await _backfill_auth_history()
        await _recover_users_from_chat_history()
        user_count = await db["users"].count_documents({})
        login_count = await db.login_events.count_documents({})
        print(f"ADMIN: MongoDB database '{MONGODB_DB_NAME}' — {user_count} users, {login_count} login events")
    except Exception as exc:
        print("ADMIN SEED WARNING:", exc)
    yield
    # Cleanup tasks can be added here if needed

app = FastAPI(lifespan=lifespan)

# CORS — allow Render frontend + local dev; patch headers on error responses too
_cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3001,http://127.0.0.1:3001,"
        "http://localhost:5173,http://127.0.0.1:5173,"
        "https://nova-ai-frontend.onrender.com",
    ).split(",")
    if origin.strip()
]
_cors_origin_regex = (
    os.getenv(
        "CORS_ORIGIN_REGEX",
        r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?|https://.*\.onrender\.com",
    )
    or ""
).strip()
_cors_origin_pattern = re.compile(_cors_origin_regex) if _cors_origin_regex else None


def _origin_is_allowed(origin: str) -> bool:
    if not origin:
        return False
    if origin in _cors_origins:
        return True
    if _cors_origin_pattern and _cors_origin_pattern.fullmatch(origin):
        return True
    return False


def _apply_cors_headers(response, origin: str):
    if origin and _origin_is_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex or None,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def ensure_cors_on_all_responses(request: Request, call_next):
    origin = request.headers.get("origin", "")
    try:
        response = await call_next(request)
    except Exception as exc:
        print("UNHANDLED ERROR:", exc)
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
    return _apply_cors_headers(response, origin)


@app.exception_handler(HTTPException)
async def http_exception_with_cors(request: Request, exc: HTTPException):
    response = JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None) or {},
    )
    return _apply_cors_headers(response, request.headers.get("origin", ""))


@app.exception_handler(Exception)
async def unhandled_exception_with_cors(request: Request, exc: Exception):
    print("GLOBAL EXCEPTION:", exc)
    response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
    return _apply_cors_headers(response, request.headers.get("origin", ""))


def _serialize_chat_doc(doc: dict) -> dict:
    if not doc:
        return doc
    out = dict(doc)
    if out.get("_id") is not None:
        out["_id"] = str(out["_id"])
    out.pop("embedding", None)
    created = out.get("timestamp") or out.get("created_at")
    if hasattr(created, "isoformat"):
        out["timestamp"] = created.isoformat()
    return out


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


def _prior_user_queries(history: Optional[list], limit: int = 3) -> List[str]:
    history = history or []
    return [
        (chat.get("user_query") or "").strip()
        for chat in history[-limit:]
        if (chat.get("user_query") or "").strip()
    ]


def _history_from_client_conversation(turns: Optional[list]) -> List[dict]:
    """Convert frontend message list into the same shape as Mongo chat history."""
    if not turns:
        return []

    normalized = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").lower()
        text = (turn.get("text") or turn.get("content") or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "role": "assistant" if role in {"ai", "assistant"} else "user",
                "text": text,
            }
        )

    paired: List[dict] = []
    pending_user = None
    for item in normalized:
        if item["role"] == "user":
            if pending_user:
                paired.append({"user_query": pending_user, "ai_response": ""})
            pending_user = item["text"]
        elif pending_user:
            paired.append({"user_query": pending_user, "ai_response": item["text"]})
            pending_user = None

    return paired


def _merge_chat_history(mongo_history: Optional[list], client_history: Optional[list]) -> List[dict]:
    """Use browser conversation when DB history is missing or shorter."""
    mongo = mongo_history or []
    client = client_history or []
    if not client:
        return mongo
    if not mongo:
        return client
    return client if len(client) > len(mongo) else mongo


def _is_local_business_query(message: str) -> bool:
    lower = (message or "").lower()
    return any(term in lower for term in LOCAL_BUSINESS_TERMS)


def _listing_format_guidance() -> str:
    return (
        "When the user asks about hotels, restaurants, classes, courses, coaching, hospitals, "
        "shops, or other local services, format EACH option like this:\n"
        "### Place name\n"
        "- **Address:** full address (or area/city if full address unavailable)\n"
        "- **Website:** URL from search results when available\n"
        "- **Phone:** number when available\n"
        "- **Details:** brief helpful note (timings, fees, rating, etc.)\n"
        "Include clickable links from search results. Do not invent addresses or phone numbers."
    )


def _prior_was_live_topic(history: Optional[list]) -> bool:
    return any(needs_live_search(q) for q in _prior_user_queries(history, 3))


def _is_standalone_question(message: str) -> bool:
    """True when the message carries enough context to be a new, self-contained question."""
    lower = (message or "").lower().strip()
    if not lower:
        return False
    if len(lower.split()) >= 12:
        return True
    if _looks_like_general_question(message):
        return True
    if is_weather_query(message) or _is_career_interview_query(message):
        return True
    if re.search(
        r"^(what|who|where|when|why|how|which|can you|tell me about|explain|describe|compare)\s+\S+\s+\S+",
        lower,
    ):
        return True
    return False


def _is_clear_topic_switch(message: str, history: Optional[list]) -> bool:
    """True when the user is obviously starting a new subject."""
    if not history:
        return False
    lower = (message or "").lower().strip()
    if any(
        phrase in lower
        for phrase in (
            "instead",
            "different question",
            "new topic",
            "forget that",
            "change topic",
            "another question",
            "unrelated",
        )
    ):
        return True
    if is_weather_query(message) or _is_career_interview_query(message):
        return True
    if _is_standalone_question(message) and not _references_prior_context(message):
        return True
    return False


def _is_likely_continuation(message: str, history: Optional[list]) -> bool:
    """True when the latest message probably continues the same conversation thread."""
    history = history or []
    if not history:
        return False
    if _is_clear_topic_switch(message, history):
        return False
    if _references_prior_context(message) or _is_vague_followup(message):
        return True
    word_count = len((message or "").strip().split())
    if word_count <= 8 and not _is_standalone_question(message):
        return True
    if _prior_was_live_topic(history) and word_count <= 12:
        return True
    return False


def _resolve_query_intent(message: str, has_files: bool, history: Optional[list] = None) -> str:
    """Classify the current user message so the assistant routes to the right brain."""
    history = history or []
    if has_files:
        return "upload"
    if is_weather_query(message):
        return "weather"
    if _is_career_interview_query(message):
        return "career"
    if _is_upload_focused_query(message, has_files=False):
        return "upload"
    if history and _is_likely_continuation(message, history):
        return "followup"
    if _needs_factual_verification(message, history):
        return "live"
    if needs_live_search(message) or _is_entertainment_release_query(message):
        return "live"
    return "general"


def _intent_guidance(intent: str) -> str:
    notes = {
        "general": (
            "Current question type: general knowledge. "
            "Answer the user's question directly. "
            "If prior turns are included, decide whether the latest message continues that thread or starts something new."
        ),
        "followup": (
            "Current question type: follow-up / continuation. "
            "The latest message is almost certainly about the same subject as the previous turn. "
            "Use conversation history plus any live search data. "
            "Infer what the user means — do NOT ask them to clarify when context already makes it clear."
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
            "Current question type: live/current or factual lookup. "
            "Use the real-time search data below when answering. "
            "Prefer facts from search results over memory. "
            "Do not invent names, lyrics, numbers, or dates. If results are incomplete, say what is confirmed."
        ),
        "career": (
            "Current question type: interview or career advice. "
            "Give practical, helpful guidance for interviews, jobs, or career preparation."
        ),
    }
    return notes.get(intent, notes["general"])


def _should_include_conversation_history(message: str, intent: str, history: Optional[list] = None) -> bool:
    """Keep prior turns when the user is continuing the same conversation thread."""
    history = history or []
    if not history:
        return False
    if _is_clear_topic_switch(message, history):
        return False
    if intent == "followup":
        return True
    if _is_likely_continuation(message, history):
        return True
    if intent == "upload" and _references_prior_context(message):
        return True
    return False


def _should_live_search(message: str, history: Optional[list], intent: str) -> bool:
    """Decide if a live web lookup will help for this turn."""
    history = history or []
    if _needs_factual_verification(message, history):
        return True
    if needs_live_search(message) or _is_entertainment_release_query(message) or _is_local_business_query(message):
        return True
    if intent == "followup" and history:
        combined = _build_live_search_query(message, history)
        if needs_live_search(combined) or _is_local_business_query(combined):
            return True
        return _prior_was_live_topic(history) or _is_local_business_query(" ".join(_prior_user_queries(history, 2)))
    if history and _is_likely_continuation(message, history):
        prior_text = " ".join(_prior_user_queries(history, 2))
        combined = f"{prior_text} {message}"
        return (
            needs_live_search(prior_text)
            or needs_live_search(combined)
            or _is_local_business_query(combined)
        )
    return False


def _is_vague_followup(message: str) -> bool:
    """Short continuation that only makes sense with prior chat context."""
    lower = (message or "").lower().strip()
    if not lower:
        return False
    word_count = len(lower.split())
    if word_count > 12:
        return False
    patterns = [
        r"^(tell me more|more details|go on|continue|elaborate|expand)\b",
        r"^(what about|how about|and what about)\b",
        r"^(currently|right now|at the moment)\b",
        r"^(this week|this month|today|now|latest)\??$",
        r"^(and |also |what else|anything else|any more|more\??)\b",
        r"^(which ones|name them|list them|give examples|examples)\b",
        r"^(like what|such as|for example)\??$",
        r"^(why|how so|what do you mean)\??$",
        r"^(it|that|this|those|these|them)\??$",
        r"^(yes|no|ok|okay|thanks|thank you)[,.!?\s]",
        r"^(can you explain|explain more|more on that)\b",
    ]
    if any(re.search(pattern, lower) for pattern in patterns):
        return True
    if word_count <= 6 and not _is_standalone_question(message):
        return True
    return False


def _is_song_lyrics_query(message: str) -> bool:
    lower = (message or "").lower()
    return any(term in lower for term in SONG_LYRICS_TERMS)


def _is_user_correction(message: str) -> bool:
    lower = (message or "").lower()
    return any(term in lower for term in CORRECTION_TERMS)


def _is_fact_lookup_query(message: str) -> bool:
    lower = (message or "").lower()
    if _is_song_lyrics_query(message):
        return True
    if any(term in lower for term in FACT_LOOKUP_TERMS):
        return True
    if any(term in lower for term in ENTERTAINMENT_TERMS) and any(
        word in lower for word in ("who", "what", "when", "where", "which", "tell me about", "about the")
    ):
        return True
    return False


def _needs_factual_verification(message: str, history: Optional[list] = None) -> bool:
    """True when the answer must come from search, not model memory."""
    history = history or []
    if _is_user_correction(message) or _is_song_lyrics_query(message) or _is_fact_lookup_query(message):
        return True
    if history:
        prior_blob = " ".join(_prior_user_queries(history, 4)).lower()
        if _is_song_lyrics_query(prior_blob) and _is_likely_continuation(message, history):
            return True
        if any(_is_user_correction(q) for q in _prior_user_queries(history, 2)):
            return True
    return False


def _is_lyrics_request(message: str, history: Optional[list] = None) -> bool:
    """True for direct lyrics queries or short affirmations after a lyrics question."""
    if _is_song_lyrics_query(message):
        return True
    history = history or []
    if not history:
        return False
    lower = (message or "").lower().strip()
    if lower in ("sure", "yes", "yes please", "ok", "okay", "go ahead", "please", "do it"):
        prior = (_prior_user_queries(history, 1) or [""])[-1]
        return _is_song_lyrics_query(prior)
    return False


def _fact_accuracy_guidance() -> str:
    return (
        "Accuracy rules (critical):\n"
        "- Answer ONLY what the user asked for — exact song, movie, recipe, person, or fact.\n"
        "- When Real-time Search Data is provided, treat it as the primary source. Do NOT invent details.\n"
        "- NEVER guess or fabricate song lyrics, cast names, dates, or which film/album a song belongs to.\n"
        "- Similar titles from different movies are different songs — verify the movie/show name the user gave.\n"
        "- If the user corrects you, discard the previous answer and follow the correction.\n"
        "- For lyrics: quote ONLY lines clearly supported by search results. If full lyrics are not verified, "
        "say so honestly and share only confirmed fragments or official links — never fill gaps with made-up text.\n"
        "- When web snippets contain partial lyrics (including Marathi/Devanagari text), combine those fragments "
        "into the fullest answer possible and name the film/song if the snippets mention it.\n"
        "- Prefer being incomplete and honest over sounding confident with wrong information."
    )


def _is_entertainment_release_query(message: str) -> bool:
    lower = (message or "").lower()
    has_entertainment = any(term in lower for term in ENTERTAINMENT_TERMS)
    has_release_intent = any(
        term in lower
        for term in (
            "latest", "recent", "released", "release", "new", "current", "currently",
            "now", "today", "this week", "this month", "upcoming", "in theater", "in theatre",
            "to watch", "what to watch", "recommend", "suggest",
        )
    )
    if has_entertainment and has_release_intent:
        return True
    if has_entertainment and any(p in lower for p in ("movies to watch", "films to watch", "movie list", "new hindi")):
        return True
    return False


def _build_live_search_query(message: str, history: Optional[list] = None) -> str:
    """Build a search query; expand short follow-ups using prior user questions."""
    history = history or []
    message = (message or "").strip()
    if not message:
        return message

    year = str(datetime.utcnow().year)
    month = datetime.utcnow().strftime("%B")
    prior_text = " ".join(_prior_user_queries(history, 4)).strip() if history else ""
    combined = f"{prior_text} {message}".strip() if prior_text else message

    if _is_user_correction(message) and prior_text:
        query = f"{combined} correct verified"
        if _is_song_lyrics_query(combined) or "lyrics" in combined.lower():
            query = f"{combined} song lyrics verified"
        return re.sub(r"\s+", " ", query).strip()

    if _is_song_lyrics_query(message):
        if (
            history
            and _is_likely_continuation(message, history)
            and not _message_names_new_song(message, history)
        ):
            prior_text = " ".join(_prior_user_queries(history, 3)).strip()
            query = f"{prior_text} {message}".strip()
        else:
            query = message
        if "lyrics" not in query.lower():
            query = f"{query} lyrics"
        return re.sub(r"\s+", " ", query).strip()

    if (
        prior_text
        and _is_song_lyrics_query(prior_text)
        and history
        and _is_likely_continuation(message, history)
        and not _message_names_new_song(message, history)
    ):
        query = combined
        if "lyrics" not in query.lower():
            query = f"{query} lyrics"
        return re.sub(r"\s+", " ", query).strip()

    if _is_fact_lookup_query(message) and prior_text:
        return re.sub(r"\s+", " ", combined).strip()

    if history and _is_likely_continuation(message, history):
        prior_text = " ".join(_prior_user_queries(history, 3)).strip()
        combined = f"{prior_text} {message}".strip()
        if _is_local_business_query(combined) or _is_local_business_query(prior_text):
            combined = f"{combined} address phone website contact details"
        elif needs_live_search(combined) or needs_live_search(prior_text):
            if year not in combined:
                combined = f"{combined} latest {month} {year}"
        return re.sub(r"\s+", " ", combined).strip()

    if needs_live_search(message):
        q = message
        if _is_local_business_query(message):
            q = f"{message} address phone website contact details"
        if year not in q and any(
            term in q.lower()
            for term in ("latest", "recent", "current", "currently", "today", "now", "this week", "this month")
        ):
            q = f"{q} {month} {year}"
        return re.sub(r"\s+", " ", q).strip()

    return message


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
        r"\b(currently released|currently release|in theaters|in theatres)\b",
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
    "lodon": "London",
    "londn": "London",
    "londom": "London",
}


POEM_CREATIVE_TERMS = (
    "poem", "poems", "poetry", "shayari", "shayri", "ghazal", "gazal",
    "verse", "verses", "rhyme", "rhymes", "couplet", "haiku", "sonnet",
)


LOCATION_STOP_WORDS = {
    "what", "whats", "is", "the", "weather", "temperature", "forecast",
    "rain", "rainy", "climate", "in", "at", "for", "near", "today", "now", "current",
    "tell", "me", "todays", "today's", "temp", "upcoming", "details", "and",
    "please", "of", "about", "conditions", "condition", "humidity", "wind",
    "let", "know", "weathe", "wether", "whether", "degree", "degrees",
    "some", "want", "i", "season", "seasons", "poem", "poems", "poetry",
    "give", "share", "send", "write", "need",
}


WEATHER_INTENT_TERMS = [
    "weather", "weathe", "wether", "temperature", "temp", "rain", "forecast",
    "climate", "humidity", "wind", "degree", "degrees",
]

LIVE_INFO_TERMS = [
    "current", "currently", "today", "todays", "today's", "now", "latest", "recent", "live", "real time",
    "breaking", "news", "update", "updates", "price", "prices", "stock", "stalk",
    "crypto", "score", "match", "result", "results", "headlines", "search",
    "who won", "what happened", "this week", "this month", "this year",
    "released", "release", "releases", "releasing", "trending", "happening",
    "bollywood", "box office", "in theaters", "in theatres", "streaming",
    "election", "poll", "weather today", "market today", "gold", "nifty", "sensex",
]

ENTERTAINMENT_TERMS = [
    "bollywood", "movie", "movies", "film", "films", "cinema", "box office",
    "hindi film", "hindi movie", "tollywood", "kollywood", "web series",
    "ott release", "theater", "theatre", "marathi", "song", "songs",
    "lyrics", "lyric", "soundtrack", "album", "gazal", "ghazal",
]

SONG_LYRICS_TERMS = [
    "song", "songs", "lyrics", "lyric", "gazal", "ghazal", "anthem", "soundtrack",
    "movie song", "film song", "full lyrics", "stanza", "opening line", "first stanza",
    "pathava", "patha", "pahije", "marathi song", "hindi song", "album track",
]

CORRECTION_TERMS = [
    "wrong", "incorrect", "not right", "that's not", "that is not", "is not correct",
    "mistake", "mixed up", "different song", "wrong song", "actually", "i meant",
    "i mean", "not about", "first stanza", "opening line", "no i want", "this is wrong",
    "that's wrong", "you gave", "not the same",
]

FACT_LOOKUP_TERMS = [
    "who wrote", "who sang", "who composed", "cast of", "director of", "plot of",
    "release date", "box office", "biography of", "history of", "definition of",
    "recipe for", "ingredients for", "how to make", "step by step",
]

LOCAL_BUSINESS_TERMS = [
    "hotel", "hotels", "hostel", "resort", "restaurant", "cafe", "class", "classes",
    "course", "courses", "coaching", "tuition", "school", "college", "institute",
    "hospital", "clinic", "doctor", "gym", "salon", "spa", "shop", "store",
    "near me", "nearby", "address", "contact", "phone", "website", "location",
    "training center", "academy", "library", "mall", "showroom",
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


def _is_poem_or_creative_request(message: str) -> bool:
    lower = (message or "").lower()
    return any(term in lower for term in POEM_CREATIVE_TERMS)


def is_weather_query(message: str) -> bool:
    if _is_poem_or_creative_request(message):
        return False

    normalized = compact_location_text(message or "").lower()

    strong_weather_terms = (
        "weather", "weathe", "wether", "temperature", "temp", "forecast",
        "climate", "humidity", "wind", "degree", "degrees",
    )
    if any(term in normalized for term in strong_weather_terms):
        return True

    # "rain" alone often means poems or monsoon talk — require a place or explicit weather context
    if "rain" in normalized or "rainy" in normalized:
        if any(w in normalized for w in ("season", "poem", "poems", "poetry", "monsoon poem")):
            return False
        if re.search(r"\b(?:weather|forecast|today|tomorrow|now|currently)\b", normalized):
            return True
        if re.search(r"\b(?:in|at|for|near)\s+[a-z]{2,}", normalized):
            return True

    return False


def _normalize_query_typos(message: str) -> str:
    """Fix common typos so intent detection still works."""
    text = (message or "").lower()
    replacements = (
        ("stalk market", "stock market"),
        ("stalk ", "stock "),
        (" maket", " market"),
        ("nifty cahnges", "nifty changes"),
        ("weathe ", "weather "),
        ("lodon", "london"),
        ("londn", "london"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def is_gold_rate_query(message: str) -> bool:
    lower = _normalize_query_typos(message)
    return "gold" in lower and any(t in lower for t in ("rate", "rates", "price", "prices", "today", "todays", "today's"))


def is_news_query(message: str) -> bool:
    lower = _normalize_query_typos(message)
    has_news = any(t in lower for t in ("news", "headlines", "breaking", "headline"))
    has_time = any(t in lower for t in ("today", "todays", "today's", "latest", "current", "now", "political"))
    return has_news and (has_time or "political" in lower)


def is_equity_market_query(message: str) -> bool:
    lower = _normalize_query_typos(message)
    keywords = (
        "equity", "stock market", "share market", "nifty", "sensex",
        "indices", "index", "stock ", " stocks", "market change", "market changes",
    )
    return any(word in lower for word in keywords)


def _clean_search_for_display(search_data: str) -> str:
    """Strip raw connection errors from text shown to users."""
    if not search_data:
        return ""
    lower = search_data.lower()
    bad_markers = (
        "search failed", "timed out", "connectionpool", "max retries",
        "connecttimeouterror", "connection to api.duckduckgo",
    )
    if any(m in lower for m in bad_markers):
        return ""
    if lower.startswith("no recent live results"):
        return ""
    return search_data.strip()


def fetch_google_news_rss(query: str, max_items: int = 8, region: str = "IN") -> str:
    """Fetch headlines from Google News RSS — reliable on cloud hosts."""
    try:
        safe_query = quote_plus(query)
        ceid = "US:en" if region.upper() == "US" else "IN:en"
        hl = "en-US" if region.upper() == "US" else "en-IN"
        rss_url = f"https://news.google.com/rss/search?q={safe_query}&hl={hl}&gl={region}&ceid={ceid}"
        rss_resp = http_session.get(rss_url, timeout=12)
        if not rss_resp.ok or not rss_resp.text:
            return ""
        root = ET.fromstring(rss_resp.text)
        items = root.findall(".//item")
        if not items:
            return ""
        news_parts = []
        for item in items[:max_items]:
            title = unescape((item.findtext("title") or "Untitled").strip())
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            news_parts.append(f"{title} [{pub}] ({link})")
        return "Latest news headlines: " + " | ".join(news_parts)
    except Exception as exc:
        print("GOOGLE NEWS RSS ERROR:", exc)
        return ""


def _format_live_headlines(title: str, search_data: str, extra: str = "") -> str:
    """Turn RSS/search text into a clean bullet list for the user."""
    headlines = []
    payload = (search_data or "").replace("Latest news headlines:", "")
    for part in re.split(r"\s*\|\s*", payload):
        part = part.strip()
        if not part or len(part) < 12:
            continue
        match = re.match(r"(.+?)\s*\[([^\]]+)\]\s*\((https?://[^\)]+)\)", part)
        if match:
            headlines.append(f"- **{match.group(1).strip()}** — _{match.group(2).strip()}_")
        elif not part.lower().startswith("wikipedia"):
            headlines.append(f"- {part[:300]}")

    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if headlines:
        body = "\n".join(headlines[:10])
        reply = f"**{title}** (as of {stamp}):\n\n{body}"
        if extra:
            reply += f"\n\n{extra}"
        return reply

    if extra:
        return f"**{title}** (as of {stamp}):\n\n{extra}"
    return ""


def get_india_gold_rate_snapshot() -> str:
    """Fetch indicative India gold rates from public sources."""
    parts = []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        resp = http_session.get(
            "https://www.goodreturns.in/gold-rates/",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            timeout=12,
        )
        if resp.ok:
            text = resp.text
            rates = []
            for label, pattern in (
                ("24 Carat (India)", r"24\s*[Cc]arat[^₹>]*₹\s*([\d,]+)"),
                ("22 Carat (India)", r"22\s*[Cc]arat[^₹>]*₹\s*([\d,]+)"),
            ):
                match = re.search(pattern, text)
                if match:
                    rates.append(f"{label}: ₹{match.group(1)} per gram")
            if rates:
                parts.append(f"India gold rates as_of {now}: " + " | ".join(rates) + " (source: Goodreturns)")
    except Exception as exc:
        print("GOLD RATE SCRAPE ERROR:", exc)

    rss = fetch_google_news_rss("gold rate today india 24 carat", max_items=5)
    if rss:
        parts.append(rss)

    if not parts:
        return "Live gold rate feed temporarily unavailable."
    return " || ".join(parts)


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
    msg = compact_location_text(_normalize_query_typos(message or ""))
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
        candidate = re.sub(
            r"\b(today|tomorrow|now|currently|right now|please|and|upcoming|forecast|details)\b.*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip(" ,.?!")
        if candidate:
            normalized = normalize_location_query(candidate)
            if normalized.lower() not in POEM_CREATIVE_TERMS and len(normalized.split()) <= 6:
                return normalized

    fallback = strip_weather_words(msg)
    if fallback and fallback.lower() not in POEM_CREATIVE_TERMS:
        word_count = len(fallback.split())
        if word_count <= 4:
            return normalize_location_query(fallback)

    return None

def ddg_html_search(query: str, max_items: int = 6) -> str:
    """Scrape DuckDuckGo HTML results — works well for song lyrics snippets."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _parse_snippets(html: str) -> List[str]:
        patterns = [
            r'class="result__snippet"[^>]*>(.*?)</(?:a|td)>',
            r'class="result-snippet"[^>]*>(.*?)</(?:a|td|div)>',
        ]
        cleaned = []
        for pattern in patterns:
            for raw in re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL):
                text = unescape(re.sub(r"<[^>]+>", " ", raw))
                text = re.sub(r"\s+", " ", text).strip()
                if text and len(text) > 8 and text not in cleaned:
                    cleaned.append(text)
                if len(cleaned) >= max_items:
                    break
            if cleaned:
                break
        return cleaned[:max_items]

    try:
        resp = http_session.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": "", "kl": ""},
            headers=headers,
            timeout=8,
        )
        if resp.ok:
            snippets = _parse_snippets(resp.text)
            if snippets:
                return " | ".join(snippets)
    except Exception as exc:
        print("DDG HTML POST ERROR:", exc)

    try:
        lite_url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"
        resp = http_session.get(lite_url, headers=headers, timeout=8)
        if resp.ok:
            snippets = _parse_snippets(resp.text)
            if snippets:
                return " | ".join(snippets)
    except Exception as exc:
        print("DDG LITE GET ERROR:", exc)

    return ""


def _lyrics_search_query_variants(query: str) -> List[str]:
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return []
    focus = _extract_song_query_focus(q) or q
    variants: List[str] = []

    def add(value: str):
        value = re.sub(r"\s+", " ", value).strip()
        if value and value.lower() not in [v.lower() for v in variants]:
            variants.append(value)

    add(f"{focus} lyrics")
    add(f"{focus} song lyrics")
    if "lyrics" not in q.lower():
        add(f"{q} lyrics")
    add(f"{focus} full lyrics")
    return variants[:4]


def _normalize_song_title(title: str) -> str:
    """Fix common spellings so search/LLM target the right song."""
    lower = (title or "").lower().strip()
    aliases = {
        "kajrare": "kajra re",
        "kajraare": "kajra re",
        "kajra re": "kajra re",
        "mansane mansashi manasasam": "mansane mansashi hich amuchi praarthana ubuntu",
        "mansane mansashi": "mansane mansashi ubuntu marathi",
        "sunya sunya maifilit mazya": "sunya sunya maifilit mazya marathi",
        "sunya sunya maifilit mzya": "sunya sunya maifilit mazya marathi",
        "toch chandrama nabhat": "toch chandrama nabhat marathi",
    }
    if lower in aliases:
        return aliases[lower]
    if lower.startswith("kajra") and "re" not in lower.split():
        return "kajra re"
    return title.strip()


def _resolve_song_focus(message: str, history: Optional[list] = None) -> str:
    history = history or []
    focus = _extract_song_query_focus(message)
    if not focus and history:
        lower = (message or "").lower().strip()
        if lower in ("sure", "yes", "yes please", "ok", "okay", "go ahead", "please", "do it"):
            focus = _extract_song_query_focus((_prior_user_queries(history, 1) or [""])[-1])
        elif _is_likely_continuation(message, history):
            focus = _extract_song_query_focus((_prior_user_queries(history, 1) or [""])[-1])
    return _normalize_song_title(focus or "")


def _effective_lyrics_message(message: str, history: Optional[list] = None) -> str:
    history = history or []
    lower = (message or "").lower().strip()
    if lower in ("sure", "yes", "yes please", "ok", "okay", "go ahead", "please", "do it") and history:
        return (_prior_user_queries(history, 1) or [message])[-1]
    return message


def web_search_lyrics(query: str) -> str:
    """Lyrics lookup — Wikipedia + optional DDG (DDG often blocked on cloud hosts)."""
    focus = _normalize_song_title(_extract_song_query_focus(query) or query)
    sections = []

    wiki = wikipedia_search(f"{focus} song", max_items=1)
    if wiki:
        sections.append(wiki)

    try:
        ddg = ddg_html_search(f"{focus} lyrics", 6)
        if ddg:
            sections.append(f"Web snippets: {ddg}")
    except Exception as exc:
        print("LYRICS DDG SKIP:", exc)

    return " || ".join(sections) if sections else ""


def _extract_song_query_focus(message: str) -> str:
    """Strip request boilerplate to isolate the likely song title in the message."""
    text = (message or "").lower()
    text = re.sub(
        r"\b(can you|can u|could you|please|plz|send me|tell me|give me|show me|share|"
        r"i want|i need|want|need|full|complete|entire|whole|song|songs|lyrics|lyric|"
        r"gana|gaan|movie|film|from|the|a|an|me|my|of|about|what|is|are|do|does|"
        r"you|u|know|have|get|find)\b",
        " ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def _message_names_new_song(message: str, history: Optional[list]) -> bool:
    """True when the user asked for a different song than the previous turn."""
    if not history:
        return True
    focus = _extract_song_query_focus(message)
    if not focus or len(focus.split()) <= 1 and focus in ("more", "rest", "all", "continue"):
        return False
    prior = (_prior_user_queries(history, 1) or [""])[-1]
    prior_focus = _extract_song_query_focus(prior)
    if not focus or not prior_focus:
        return bool(focus)
    if focus == prior_focus:
        return False
    current_words = {w for w in focus.split() if len(w) > 2}
    prior_words = {w for w in prior_focus.split() if len(w) > 2}
    if not current_words:
        return False
    return not (current_words & prior_words)


def _lyrics_search_query(message: str, history: Optional[list] = None) -> str:
    """Build a web search query for lyrics using only the relevant song context."""
    history = history or []
    focus = _extract_song_query_focus(message)
    if not focus and history and _is_likely_continuation(message, history):
        focus = _extract_song_query_focus((_prior_user_queries(history, 1) or [""])[-1])
    if focus:
        return f"{focus} lyrics"
    if _message_names_new_song(message, history):
        q = message.strip()
    else:
        q = _build_live_search_query(message, history)
    return q if "lyrics" in q.lower() else f"{q} lyrics"


def _is_lyrics_refusal(text: str) -> bool:
    lower = (text or "").lower()
    refusal_phrases = (
        "sorry, i can't",
        "sorry, i cannot",
        "i'm sorry, i cannot",
        "i am sorry, i cannot",
        "can't provide the full lyrics",
        "cannot provide the full lyrics",
        "can't provide the lyrics",
        "cannot provide the lyrics",
        "don't have the lyrics",
        "do not have the lyrics",
        "unable to provide",
        "unable to retrieve",
        "how about i summarize",
        "can't do that",
        "cannot do that",
        "i don't have the exact lyrics",
        "i do not have the exact lyrics",
        "i don't have the lyrics",
        "i do not have the lyrics",
    )
    return any(phrase in lower for phrase in refusal_phrases)


def _parse_search_snippets(search_data: str) -> List[str]:
    if not search_data:
        return []
    payload = search_data
    for prefix in ("Web snippets: ", "Web lyrics snippets for '", "Web lyrics snippets: ", "Wikipedia — "):
        payload = payload.replace(prefix, " ")
    payload = payload.replace(" || ", " | ")
    lines = [s.strip() for s in re.split(r"\s*\|\s*", payload) if s.strip()]
    unique = []
    for line in lines:
        if line not in unique and len(line) > 10:
            unique.append(line)
    return unique


def _snippet_looks_like_lyrics(text: str) -> bool:
    if re.search(r"[\u0900-\u097F]{12,}", text or ""):
        return True
    lower = (text or "").lower()
    if any(ch in text for ch in ("…", "...", " - ", " – ")) and len(text) > 40:
        return True
    lyric_hints = ("verse", "stanza", "chorus", "lyric", "singer", "composed", "written by")
    if any(h in lower for h in lyric_hints) and len(text) > 50:
        return True
    if len(text) > 30 and text.count("http") == 0 and text.count(".") < 6:
        if len(text.split()) >= 5:
            return True
    return False


def _format_lyrics_from_search(search_data: str, song_focus: str) -> Optional[str]:
    """Format web search snippets into a lyrics-style reply."""
    snippets = _parse_search_snippets(search_data)
    if not snippets:
        return None

    lyricish = [s for s in snippets if _snippet_looks_like_lyrics(s)]
    chosen = lyricish if lyricish else snippets
    body = "\n\n".join(chosen[:8])
    title = song_focus or "your song"
    lang_hint = "Marathi" if re.search(r"[\u0900-\u097F]", body) else "Hindi/English"
    return (
        f"**{title.title()}** — lyrics from web search ({lang_hint}):\n\n"
        f"{body}\n\n"
        "*Compiled from public web snippets. If any line looks incomplete, say the movie/artist name and I will search again.*"
    )


def _format_best_effort_lyrics(message: str, search_data: str, song_focus: str) -> str:
    formatted = _format_lyrics_from_search(search_data, song_focus)
    if formatted:
        return formatted
    title = song_focus or _extract_song_query_focus(message) or message.strip()
    if search_data and len(search_data.strip()) > 40:
        snippets = _parse_search_snippets(search_data)
        body = "\n\n".join(f"- {s}" for s in snippets[:6])
        return (
            f"Here is what I found for **{title}**:\n\n{body}\n\n"
            "*Could not verify full lyrics from search alone. Share the movie/album or singer if you have it.*"
        )
    return (
        f"I searched for **{title}** but could not fetch lyrics right now.\n\n"
        "Please try again with the **movie/album or singer name**, or check YouTube / JioSaavn / Google for the official audio."
    )


async def _resolve_lyrics_via_llm(message: str, search_data: str, song_focus: str) -> Optional[str]:
    """LLM lyrics lookup — primary source for any song title."""
    try:
        ai_client = get_github_models_client()
        context_block = search_data.strip() if search_data else ""
        song_label = song_focus or _extract_song_query_focus(message) or message

        def _call():
            user_parts = [
                f"Give me the full lyrics for this song: **{song_label}**",
                f"Original request: {message}",
            ]
            if context_block:
                user_parts.append(f"Reference data from web:\n{context_block}")
            user_parts.append(
                "Write every verse and chorus you know for THIS song only. "
                "Use the song's original language (Marathi/Hindi/etc.) where appropriate. "
                "Format with Markdown."
            )

            return ai_client.complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Nova AI, a helpful assistant that provides song lyrics when users ask.\n"
                            "When a user names a song, output the lyrics for that exact song.\n"
                            "Use your knowledge of published songs. Never swap in a different song.\n"
                            "Be helpful — provide the fullest lyrics you can. Do not refuse or offer only metadata."
                        ),
                    },
                    {"role": "user", "content": "\n\n".join(user_parts)},
                ],
                model="gpt-4o",
                connection_timeout=12,
                read_timeout=28,
            )

        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=35.0)
        if not response or not getattr(response, "choices", None):
            return None
        content = (response.choices[0].message.content or "").strip()
        if len(content) < 30 or _is_lyrics_refusal(content):
            return None
        return content
    except Exception as exc:
        print("LYRICS LLM ERROR:", exc)
        return None


async def _resolve_lyrics_reply(message: str, history: Optional[list], timeout: float = 6.0) -> str:
    """Global lyrics handler for ANY song: LLM first, then web, always returns an answer."""
    song_focus = _resolve_song_focus(message, history)
    effective = _effective_lyrics_message(message, history)

    search_data = ""
    search_task = asyncio.create_task(
        asyncio.to_thread(web_search_lyrics, f"{song_focus or effective} lyrics")
    )
    llm_task = asyncio.create_task(_resolve_lyrics_via_llm(effective, "", song_focus))

    try:
        search_data = await asyncio.wait_for(search_task, timeout=timeout)
    except asyncio.TimeoutError:
        search_task.cancel()

    llm_reply = await llm_task
    if llm_reply:
        print("🎵 LYRICS: LLM")
        return llm_reply

    if search_data:
        llm_with_search = await _resolve_lyrics_via_llm(effective, search_data, song_focus)
        if llm_with_search:
            print("🎵 LYRICS: LLM + web context")
            return llm_with_search
        formatted = _format_lyrics_from_search(search_data, song_focus)
        if formatted:
            print("🎵 LYRICS: web snippets")
            return formatted

    print("🎵 LYRICS: best-effort for:", (song_focus or message)[:80])
    return _format_best_effort_lyrics(effective, search_data, song_focus)


def wikipedia_search(query: str, max_items: int = 2) -> str:
    """Free factual lookup via Wikipedia API (good for films, songs, people)."""
    try:
        api = "https://en.wikipedia.org/w/api.php"
        search_resp = http_session.get(
            api,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "utf8": 1,
                "srlimit": max(1, min(max_items, 3)),
            },
            timeout=8,
        )
        if not search_resp.ok:
            return ""
        hits = (search_resp.json().get("query") or {}).get("search") or []
        if not hits:
            return ""

        parts = []
        for hit in hits[:max_items]:
            title = hit.get("title") or ""
            snippet = unescape(re.sub(r"<[^>]+>", "", hit.get("snippet") or "")).strip()
            if not title:
                continue
            extract_resp = http_session.get(
                api,
                params={
                    "action": "query",
                    "prop": "extracts",
                    "explaintext": 1,
                    "exintro": 0,
                    "titles": title,
                    "format": "json",
                    "redirects": 1,
                },
                timeout=8,
            )
            extract = ""
            if extract_resp.ok:
                pages = (extract_resp.json().get("query") or {}).get("pages") or {}
                for page in pages.values():
                    extract = (page.get("extract") or "").strip()
                    if extract:
                        extract = extract[:1200]
                        break
            if extract:
                parts.append(f"Wikipedia — {title}: {extract}")
            elif snippet:
                parts.append(f"Wikipedia — {title}: {snippet}")

        return " | ".join(parts) if parts else ""
    except Exception as exc:
        print("WIKIPEDIA SEARCH ERROR:", exc)
        return ""


def web_search(query: str, max_items: int = 3):
    """No-key live web/news lookup — Google News RSS first; DDG is optional."""
    if _is_song_lyrics_query(query):
        return web_search_lyrics(query)

    item_limit = max(3, min(max_items, 10))
    sections = []
    lower_q = (query or "").lower()

    if any(term in lower_q for term in SONG_LYRICS_TERMS + list(ENTERTAINMENT_TERMS) + list(FACT_LOOKUP_TERMS)):
        try:
            wiki = wikipedia_search(query, max_items=2)
            if wiki:
                sections.append(wiki)
        except Exception as exc:
            print("WEB SEARCH WIKI ERROR:", exc)

    rss = fetch_google_news_rss(query, max_items=item_limit)
    if rss:
        sections.append(rss)

    try:
        safe_query = quote_plus(query)
        ddg_url = f"https://api.duckduckgo.com/?q={safe_query}&format=json&no_html=1&no_redirect=1"
        ddg_resp = http_session.get(ddg_url, timeout=4)
        if ddg_resp.ok:
            ddg = ddg_resp.json()
            abstract = (ddg.get("AbstractText") or "").strip()
            abstract_url = (ddg.get("AbstractURL") or "").strip()
            if abstract:
                sections.append(f"DuckDuckGo: {abstract} ({abstract_url})")
            related = ddg.get("RelatedTopics") or []
            snippets = []
            for t in related[:item_limit]:
                if isinstance(t, dict) and t.get("Text"):
                    snippets.append(t.get("Text"))
            if snippets:
                sections.append("DuckDuckGo related: " + " | ".join(snippets))
    except Exception as exc:
        print("WEB SEARCH DDG SKIP:", exc)

    if sections:
        return " || ".join(sections)

    return "No recent live results found from available providers."

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


def require_admin(user=Depends(get_current_user)):
    if not _is_admin_email(_normalize_email(user.get("email"))):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


async def _chat_counts_by_email() -> dict:
    """Count chats per registered email (case-insensitive)."""
    counts: dict = {}
    pipeline = [
        {"$match": {"user_email": {"$exists": True, "$type": "string", "$ne": ""}}},
        {"$project": {"email_key": {"$toLower": "$user_email"}}},
        {"$match": {"email_key": {"$not": {"$regex": "^guest:"}}}},
        {"$group": {"_id": "$email_key", "count": {"$sum": 1}}},
    ]
    async for row in db.chat_history.aggregate(pipeline):
        counts[row.get("_id") or ""] = row.get("count") or 0
    return counts


async def _chat_activity_rows(limit: int = 500) -> list:
    """All chat identities from MongoDB — registered emails, guests, and recovered."""
    registered = set()
    async for doc in db["users"].find({}, {"email": 1}):
        email = _normalize_email(doc.get("email"))
        if email:
            registered.add(email)

    pipeline = [
        {"$match": {"user_email": {"$exists": True, "$type": "string", "$ne": ""}}},
        {
            "$group": {
                "_id": "$user_email",
                "chat_count": {"$sum": 1},
                "first_seen": {"$min": "$timestamp"},
                "last_seen": {"$max": "$timestamp"},
            }
        },
        {"$sort": {"last_seen": -1}},
        {"$limit": max(1, min(limit, 1000))},
    ]

    rows = []
    async for row in db.chat_history.aggregate(pipeline):
        identity = row.get("_id") or ""
        is_guest = str(identity).lower().startswith("guest:")
        email_key = _normalize_email(identity) if not is_guest and "@" in identity else ""
        rows.append({
            "identity": identity,
            "email": email_key or None,
            "type": "guest" if is_guest else ("registered" if email_key in registered else "chat_only"),
            "chat_count": row.get("chat_count") or 0,
            "first_seen": _format_admin_datetime(row.get("first_seen")),
            "last_seen": _format_admin_datetime(row.get("last_seen")),
            "registered": email_key in registered if email_key else False,
        })
    return rows


async def _recover_users_from_chat_history():
    """Create user + login records for real emails found in chat_history but missing from users."""
    registered = set()
    async for doc in db["users"].find({}, {"email": 1}):
        email = _normalize_email(doc.get("email"))
        if email:
            registered.add(email)

    pipeline = [
        {"$match": {"user_email": {"$exists": True, "$type": "string", "$ne": ""}}},
        {"$project": {"email_key": {"$toLower": "$user_email"}}},
        {"$match": {
            "$and": [
                {"email_key": {"$regex": "@"}},
                {"email_key": {"$not": {"$regex": "^guest:"}}},
            ]
        }},
        {
            "$group": {
                "_id": "$email_key",
                "first_seen": {"$min": "$timestamp"},
                "last_seen": {"$max": "$timestamp"},
                "chat_count": {"$sum": 1},
            }
        },
    ]

    recovered = 0
    async for row in db.chat_history.aggregate(pipeline):
        email = row.get("_id") or ""
        if not email or email in registered:
            continue
        first_seen = row.get("first_seen") or datetime.utcnow()
        last_seen = row.get("last_seen") or first_seen
        username = email.split("@")[0]
        await db["users"].insert_one({
            "username": username,
            "email": email,
            "password": None,
            "auth_provider": "recovered_from_chat",
            "created_at": first_seen,
            "last_login_at": last_seen,
            "login_count": 0,
            "recovered_from_chat": True,
            "chat_count_at_recovery": row.get("chat_count") or 0,
        })
        await db.login_events.insert_one({
            "email": email,
            "username": username,
            "event": "chat_recovered",
            "auth_provider": "recovered_from_chat",
            "created_at": first_seen,
        })
        registered.add(email)
        recovered += 1

    if recovered:
        print(f"ADMIN: Recovered {recovered} user(s) from chat_history")


def _utc_day_bounds(days_ago: int = 0):
    """UTC midnight window. days_ago=0 today, 1 yesterday."""
    now = datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day) - timedelta(days=days_ago)
    return day_start, day_start + timedelta(days=1)


def _parse_optional_date(value: Optional[str]) -> Optional[datetime]:
    if not value or not str(value).strip():
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date. Use YYYY-MM-DD.")


def _admin_date_filter(period: Optional[str], since: Optional[str], until: Optional[str], field: str = "created_at") -> dict:
    """Build Mongo date range for admin filters."""
    period = (period or "").strip().lower()
    if period == "today":
        start, end = _utc_day_bounds(0)
        return {field: {"$gte": start, "$lt": end}}
    if period == "yesterday":
        start, end = _utc_day_bounds(1)
        return {field: {"$gte": start, "$lt": end}}
    if period in ("7days", "week", "last7"):
        return {field: {"$gte": datetime.utcnow() - timedelta(days=7)}}

    since_dt = _parse_optional_date(since)
    until_dt = _parse_optional_date(until)
    if not since_dt and not until_dt:
        return {}

    rng = {}
    if since_dt:
        rng["$gte"] = since_dt
    if until_dt:
        rng["$lt"] = until_dt + timedelta(days=1)
    return {field: rng}


def _admin_user_activity_filter(period: Optional[str], since: Optional[str], until: Optional[str]) -> dict:
    """Match users who signed up or logged in during the selected window."""
    created = _admin_date_filter(period, since, until, "created_at")
    if not created:
        return {}
    logged_in = _admin_date_filter(period, since, until, "last_login_at")
    return {"$or": [created, logged_in]}


def _format_admin_datetime(value) -> Optional[str]:
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def record_auth_event(email: str, event: str, auth_provider: str, username: str = ""):
    """Track sign-up and login events for the admin dashboard."""
    email = (email or "").strip().lower()
    if not email:
        return
    now = datetime.utcnow()
    try:
        await db.login_events.insert_one({
            "email": email,
            "username": username or "",
            "event": event,
            "auth_provider": auth_provider or "email",
            "created_at": now,
        })
        if event in ("register", "google_register"):
            await db["users"].update_one(
                _email_lookup_filter(email),
                {"$set": {"last_login_at": now, "login_count": 1}},
            )
        else:
            await db["users"].update_one(
                _email_lookup_filter(email),
                {"$set": {"last_login_at": now}, "$inc": {"login_count": 1}},
            )
    except Exception as exc:
        print("AUTH EVENT LOG ERROR:", exc)


async def _ensure_admin_indexes():
    try:
        await db.login_events.create_index([("created_at", -1)])
        await db.login_events.create_index([("email", 1)])
        await db["users"].create_index([("created_at", -1)])
        await db["users"].create_index([("last_login_at", -1)])
    except Exception as exc:
        print("ADMIN INDEX WARNING:", exc)


async def _backfill_auth_history():
    """Seed register events, normalize emails, and repair missing profile dates."""
    try:
        async for user in db["users"].find({}):
            email_raw = (user.get("email") or "").strip()
            email = _normalize_email(email_raw)
            if not email:
                continue

            fixes = {}
            if email_raw != email:
                fixes["email"] = email
            created = user.get("created_at")
            if not created:
                created = _object_id_timestamp(user)
                fixes["created_at"] = created
            if not user.get("auth_provider"):
                fixes["auth_provider"] = "google" if user.get("google_id") else "email"
            if fixes:
                await db["users"].update_one({"_id": user["_id"]}, {"$set": fixes})

            event = "google_register" if (user.get("auth_provider") == "google" or fixes.get("auth_provider") == "google") else "register"
            exists = await db.login_events.find_one({
                "email": email,
                "event": {"$in": ["register", "google_register"]},
            })
            if not exists:
                await db.login_events.insert_one({
                    "email": email,
                    "username": user.get("username") or "",
                    "event": event,
                    "auth_provider": user.get("auth_provider") or fixes.get("auth_provider") or "email",
                    "created_at": created,
                })
            if not user.get("last_login_at"):
                await db["users"].update_one(
                    {"_id": user["_id"]},
                    {
                        "$set": {
                            "last_login_at": created,
                            "login_count": user.get("login_count") or 1,
                        }
                    },
                )
    except Exception as exc:
        print("AUTH BACKFILL WARNING:", exc)


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
    client_conversation = []
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
        client_conversation = payload.get("conversation") or payload.get("recent_turns") or []

    if not message:
        raise HTTPException(status_code=400, detail="Chat message is required.")
    return await run_chat_message(message, email, session_id, files, client_conversation=client_conversation)


async def run_chat_message(
    message: str,
    email: Optional[str],
    session_id: Optional[str],
    files: list,
    client_conversation: Optional[list] = None,
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

        client_history = _history_from_client_conversation(client_conversation)
        recent_history = _merge_chat_history(recent_history, client_history)
        
        # Detect real-time data needs
        real_time_context = ""
        message_lower = message.lower()
        normalized_lower = _normalize_query_typos(message)
        direct_reply = None
        lyrics_direct = None
        direct_sources = []
        generic_document_query = _is_generic_document_query(message) or bool(files)
        upload_focused_query = _is_upload_focused_query(message, has_files=bool(files))
        query_intent = _resolve_query_intent(message, has_files=bool(files), history=recent_history)
        include_history = _should_include_conversation_history(message, query_intent, recent_history)
        history_for_prompt = recent_history if include_history else []
        live_search_query = _build_live_search_query(message, recent_history)
        print(f"🧠 CHAT INTENT: {query_intent}, upload_focused={upload_focused_query}, include_history={include_history}, live_query='{live_search_query[:120]}'")

        # Global lyrics path — always answer here (web search + LLM), never fall through to blocking prompts
        if _is_lyrics_request(message, recent_history) and not files:
            lyrics_fast = await _resolve_lyrics_reply(message, recent_history, timeout=10.0)
            sources = [{"source": "Lyrics lookup (web + AI)", "page": None, "score": None}]
            chat_doc = {
                "user_email": user_email,
                "user_query": message,
                "ai_response": lyrics_fast,
                "embedding": [],
                "session_id": session_id,
                "timestamp": datetime.utcnow(),
                "sources": sources,
                "real_time_context": "",
                "rag_context": "",
            }
            try:
                await db.chat_history.insert_one(chat_doc)
            except Exception as e:
                print(f"CHAT HISTORY SAVE ERROR: {e}")
            response_payload = {"reply": lyrics_fast, "sources": sources}
            if session_id:
                response_payload["session_id"] = session_id
            return response_payload

        is_equity_query = is_equity_market_query(message)
        home_terms = ["home", "house", "property", "real estate", "apartment", "flat", "housing"]
        rate_terms = ["rate", "rates", "price", "prices", "market", "cost", "valuation", "situation"]
        is_home_market_query = (
            any(word in normalized_lower for word in home_terms)
            and any(word in normalized_lower for word in rate_terms)
        )

        if is_gold_rate_query(message):
            gold_data = get_india_gold_rate_snapshot()
            headline_data = fetch_google_news_rss("gold rate today india 24 carat", max_items=6)
            direct_reply = _format_live_headlines(
                "Today's gold rate (India)",
                headline_data,
                extra=gold_data if "India gold rates" in gold_data else "",
            )
            if not direct_reply:
                direct_reply = (
                    f"As of {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}:\n{gold_data}\n\n"
                    "*Source: Goodreturns / Google News RSS*"
                )
            direct_sources = ["Goodreturns", "Google News RSS"]

        elif _is_entertainment_release_query(message):
            month_year = datetime.utcnow().strftime("%B %Y")
            search_data = fetch_google_news_rss(f"latest hindi movies released {month_year}", max_items=10)
            if not search_data:
                search_data = fetch_google_news_rss("bollywood new movie releases", max_items=10)
            direct_reply = _format_live_headlines(
                f"Latest Hindi movies to watch ({month_year})",
                search_data,
                extra="*Check BookMyShow or IMDb for trailers and showtimes near you.*",
            )
            if not direct_reply:
                direct_reply = (
                    f"Live movie listings are limited right now. "
                    f"Check BookMyShow or IMDb for **Hindi releases in {month_year}**."
                )
            direct_sources = ["Google News RSS"]

        elif is_news_query(message):
            region = "US" if any(w in normalized_lower for w in ("uk", "london", "britain", "usa", "america")) else "IN"
            search_data = fetch_google_news_rss(message, max_items=10, region=region)
            if not search_data:
                search_data = _clean_search_for_display(web_search(message, max_items=8))
            direct_reply = _format_live_headlines("Today's news headlines", search_data or "")
            if not direct_reply:
                direct_reply = "News feeds are slow right now. Try BBC, Reuters, or Google News."
            direct_sources = ["Google News RSS"]

        elif is_equity_query:
            equity_data = get_india_equity_market_snapshot()
            search_data = _clean_search_for_display(
                web_search(f"nifty sensex market news {datetime.utcnow().strftime('%B %Y')}", max_items=4)
            )
            direct_reply = (
                f"As of {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}, India equity snapshot:\n"
                f"{equity_data}"
            )
            if search_data:
                news_block = _format_live_headlines("Related market news", search_data)
                if news_block:
                    direct_reply += f"\n\n{news_block}"
            direct_sources = ["NSE", "BSE", "Google News RSS"]

        elif is_home_market_query and any(x in normalized_lower for x in ["india", "indian"]):
            home_data = get_india_home_market_snapshot()
            search_data = _clean_search_for_display(web_search(message, max_items=4))
            direct_reply = (
                f"As of {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}, India home market snapshot:\n"
                f"{home_data}"
            )
            if search_data:
                direct_reply += f"\n\nRelated context:\n{search_data}"
            direct_sources = ["Numbeo", "Google News RSS"]

        elif _is_local_business_query(message):
            search_data = fetch_google_news_rss(f"{message} zomato google maps", max_items=8)
            if not search_data:
                try:
                    search_data = _clean_search_for_display(
                        await asyncio.wait_for(asyncio.to_thread(web_search, message, 8), timeout=12.0)
                    )
                except asyncio.TimeoutError:
                    search_data = ""
            direct_reply = _format_live_headlines(
                f"Results for: {message}",
                search_data or "",
                extra=(
                    "*For verified addresses, ratings, and phone numbers, search the same area on "
                    "**Google Maps** or **Zomato** — do not rely on unverified numbers.*"
                ),
            )
            if not direct_reply:
                direct_reply = (
                    f"I couldn't fetch live listings for **{message}** right now.\n\n"
                    "Open **Google Maps** or **Zomato** and search that area for up-to-date restaurant details."
                )
            direct_sources = ["Google News RSS"]

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
        
        elif _should_live_search(message, recent_history, query_intent) and not real_time_context:
            search_target = live_search_query or message
            max_items = 8 if _is_local_business_query(search_target) else 6
            if _needs_factual_verification(message, recent_history):
                max_items = 8
            if not (_is_local_business_query(search_target) or query_intent == "followup" or _is_likely_continuation(message, recent_history) or needs_live_search(search_target) or _needs_factual_verification(message, recent_history)):
                max_items = 5
            try:
                search_data = await asyncio.wait_for(
                    asyncio.to_thread(web_search, search_target, max_items),
                    timeout=20.0,
                )
            except asyncio.TimeoutError:
                print("WEB SEARCH TIMEOUT:", search_target[:120])
                if _is_song_lyrics_query(search_target):
                    search_data = await asyncio.to_thread(ddg_html_search, search_target, 8)
                    search_data = (
                        f"Web lyrics snippets: {search_data}"
                        if search_data
                        else "Lyrics search timed out. Try asking with the movie name."
                    )
                else:
                    search_data = fetch_google_news_rss(search_target, max_items=6) or "Real-time search timed out. Please try again."
            search_data = _clean_search_for_display(search_data) or search_data
            real_time_context = f"\n[{get_current_info()}]\n[Real-time Search Data: {search_data}]"
            if _is_song_lyrics_query(message):
                lyrics_direct = _format_lyrics_from_search(
                    search_data, _extract_song_query_focus(message) or message
                )
        
        # Build conversation context
        system_prompt = f"""You are a helpful AI assistant with access to real-time information and optional uploaded document context.

Think like a human in a conversation:
1. Read the prior turns (if any) and the latest user message together.
2. Decide whether the latest message **continues the same topic** or **starts a new topic**.
3. Answer what the user actually wants — give the **exact** song, fact, recipe, or detail they asked for.

Rules:
- **Continuation:** short replies ("currently?", "what about India?", "tell me more", "and price?", "yes full lyrics") refer to the previous question — answer in that context.
- **New topic:** a full standalone question on a different subject should be answered on its own without mixing old topics.
- **Corrections:** if the user says your answer was wrong or gives the correct opening line/stanza, accept the correction and answer again for what THEY specified.
- **Live/current info:** when Real-time Search Data is provided below, prefer it over memory; do not invent facts.
- **General knowledge:** answer clearly in helpful Markdown when no live data is needed, but still do not guess specific lyrics, quotes, or names.
- Wrap shell/terminal commands in inline backticks or fenced ``` code blocks.
- Only use uploaded document context when the question is about uploaded files.

{_fact_accuracy_guidance()}

{_listing_format_guidance() if (_is_local_business_query(message) or (recent_history and _is_local_business_query(" ".join(_prior_user_queries(recent_history, 2))))) else ""}

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


        if _is_user_correction(message) and history_for_prompt:
            user_content = (
                f"The user is correcting a previous answer. Their correction: {message}\n"
                f"Use the search data and give the EXACT fact/song/lyrics they want. "
                f"Do not repeat the wrong answer. Do not invent lyrics."
            )
        elif query_intent == "followup" and history_for_prompt:
            prior_topic = (history_for_prompt[-1].get("user_query") or "").strip()
            if _is_song_lyrics_query(message) or _is_song_lyrics_query(prior_topic):
                user_content = (
                    f"Follow-up about \"{prior_topic}\": {message}\n"
                    f"(Give the exact lyrics or song details requested. Use search data. Do not guess.)"
                )
            else:
                user_content = (
                    f"Follow-up on our previous discussion about \"{prior_topic}\": {message}\n"
                    f"(Give more detail on that same topic. Do not ask me to clarify.)"
                )
        else:
            user_content = message
        messages.append({"role": "user", "content": user_content})

        # Get AI Response with full context
        if direct_reply:
            ai_reply = direct_reply
            sources.extend([{"source": s, "page": None, "score": None} for s in direct_sources])
        elif lyrics_direct:
            ai_reply = lyrics_direct
            sources.append({"source": "DuckDuckGo lyrics search", "page": None, "score": None})
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
                ai_timeout = 45 if _needs_factual_verification(message, recent_history) else 20
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

    cursor = db.chat_history.find({"user_email": email}).sort("timestamp", -1).limit(20)
    messages = await cursor.to_list(length=20)

    return [_serialize_chat_doc(m) for m in messages]


@app.get("/history/session/{session_id}")
async def get_session_history(session_id: str):
    """Return messages for a given session_id (useful for guest/unauthenticated frontend)."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    cursor = db.chat_history.find({"session_id": session_id}).sort("timestamp", -1)
    messages = await cursor.to_list(length=100)

    return [_serialize_chat_doc(m) for m in messages]


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
    query = (query or "").strip()
    if not query:
        return []

    results = []
    seen_ids = set()

    def add_rows(rows):
        for row in rows or []:
            sid = str(row.get("_id", ""))
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                results.append(row)

    try:
        terms = [t for t in re.split(r"\s+", query) if t]
        if terms:
            regex = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
            keyword_rows = await db.chat_history.find({
                "user_email": email,
                "$or": [
                    {"user_query": regex},
                    {"ai_response": regex},
                ],
            }).sort("timestamp", -1).limit(10).to_list(length=10)
            add_rows(keyword_rows)

        if len(results) < 5:
            try:
                query_vector = await asyncio.wait_for(
                    asyncio.to_thread(build_embedding, query),
                    timeout=10.0,
                )
                if query_vector is not None:
                    pipeline = [
                        {
                            "$vectorSearch": {
                                "index": "vector_index",
                                "path": "embedding",
                                "queryVector": query_vector,
                                "numCandidates": 50,
                                "limit": 5,
                                "filter": {"user_email": email},
                            }
                        }
                    ]
                    vector_rows = await db.chat_history.aggregate(pipeline).to_list(length=5)
                    add_rows(vector_rows)
            except Exception as exc:
                print("VECTOR SEARCH SKIP:", exc)

        return [_serialize_chat_doc(r) for r in results]
    except Exception as e:
        print("SEARCH ERROR:", e)
        return []


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
async def health_status(verify_ai: bool = False):
    """Detailed stack health check. Pass ?verify_ai=1 to live-test GitHub Models (slow)."""
    mongo_ok = False
    mongo_error = None
    try:
        await asyncio.wait_for(db.command("ping"), timeout=5.0)
        mongo_ok = True
    except asyncio.TimeoutError:
        mongo_error = "MongoDB ping timed out after 5s"
    except Exception as exc:
        mongo_error = str(exc)

    github_token = (os.getenv("GITHUB_TOKEN") or "").strip()
    github_token_configured = bool(github_token) and not github_token.startswith("replace_with_")

    github_token_ok = None
    github_token_error = None
    if not github_token_configured:
        github_token_ok = False
        github_token_error = "GITHUB_TOKEN is missing or placeholder"
    elif verify_ai:
        try:
            def _probe_github_models():
                client = get_github_models_client()
                return client.complete(
                    messages=[{"role": "user", "content": "Reply with OK"}],
                    model="gpt-4o-mini",
                    max_tokens=3,
                    connection_timeout=5,
                    read_timeout=8,
                )

            response = await asyncio.wait_for(asyncio.to_thread(_probe_github_models), timeout=12.0)
            github_token_ok = bool(response and getattr(response, "choices", None))
        except asyncio.TimeoutError:
            github_token_ok = False
            github_token_error = "GitHub Models probe timed out"
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
    else:
        github_token_ok = None
        github_token_error = "Skipped live probe (use ?verify_ai=1)"

    neo4j_ok = False
    neo4j_error = None
    if str(os.getenv("NEO4J_ENABLED", "true")).strip().lower() in {"0", "false", "no", "off"}:
        neo4j_error = "Neo4j disabled (NEO4J_ENABLED=false)"
    else:
        try:
            driver = get_neo4j_driver()
            if driver is None:
                neo4j_error = "Neo4j not configured"
            else:
                async with driver.session() as session:
                    result = await asyncio.wait_for(session.run("RETURN 1 AS v"), timeout=5.0)
                    record = await result.single()
                    neo4j_ok = record is not None and record.get("v") == 1
        except asyncio.TimeoutError:
            neo4j_error = "Neo4j query timed out after 5s"
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


class UserRegister(BaseModel):
    username: str
    email: str # Use str first to test, or EmailStr if you have the library
    password: str = Field(..., min_length=8)
    
@app.post("/register")
async def register_user(user: UserRegister):
    try:
        email = user.email.strip().lower()
        # 1. Check if email already exists in MongoDB
        existing_user = await db["users"].find_one(_email_lookup_filter(email))
        if existing_user:
            raise HTTPException(status_code=400, detail="An account with this email already exists. Please log in instead.")

        # 2. Hash the password before saving
        hashed_password = get_password_hash(user.password)

        new_user = {
            "username": user.username,
            "email": email,
            "password": hashed_password,
            "auth_provider": "email",
            "created_at": datetime.utcnow(),
        }

        await db["users"].insert_one(new_user)
        await record_auth_event(email, "register", "email", user.username)
        token = create_access_token({"email": email})
        return _auth_response(new_user, token)
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
        "email": _normalize_email(email),
        "username": data.get("name") or email.split("@")[0],
        "avatar_url": data.get("picture"),
        "google_id": data.get("sub"),
        "email_verified": data.get("email_verified", True),
    }


@app.get("/config/public")
async def public_config():
    client_id = _google_client_id()
    return {"google_client_id": client_id}


@app.post("/auth/google")
async def google_auth(payload: GoogleAuthRequest):
    google_client_id = _google_client_id()
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
        token_aud = (token_data.get("aud") or "").strip()
        if token_aud != google_client_id:
            print(f"GOOGLE AUD MISMATCH: expected={google_client_id[:12]}... got={token_aud[:12]}...")
            raise HTTPException(status_code=400, detail="Google sign-in could not be verified for this app.")

        email = _normalize_email(token_data.get("email"))
        if not email:
            raise HTTPException(status_code=400, detail="Google did not provide an email address.")

        if token_data.get("email_verified") not in (True, "true", "True", "1", 1):
            raise HTTPException(status_code=400, detail="Please verify your Google email before signing in.")

        username = token_data.get("name") or email.split("@")[0]
        avatar_url = token_data.get("picture")
        google_id = token_data.get("sub")
    else:
        raise HTTPException(status_code=400, detail="Google sign-in token missing. Please try again.")

    existing_user = await db["users"].find_one(_email_lookup_filter(email))
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
        await record_auth_event(email, "google_register", "google", username)
    else:
        patch = {"auth_provider": existing_user.get("auth_provider") or "google"}
        if avatar_url:
            patch["avatar_url"] = avatar_url
        if google_id:
            patch["google_id"] = google_id
        if not existing_user.get("username"):
            patch["username"] = username
        if _normalize_email(existing_user.get("email")) != email:
            patch["email"] = email
        await db["users"].update_one({"_id": existing_user["_id"]}, {"$set": patch})
        await record_auth_event(email, "google_login", "google", username)
        existing_user = await db["users"].find_one({"_id": existing_user["_id"]})
        username = existing_user.get("username") or username

    token = create_access_token({"email": email})
    user_doc = await db["users"].find_one(_email_lookup_filter(email))
    return _auth_response(user_doc, token)


class ProfileUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None


def _serialize_user(doc: dict) -> dict:
    if not doc:
        return {}
    email = doc.get("email") or ""
    return {
        "username": doc.get("username") or "",
        "email": email,
        "auth_provider": doc.get("auth_provider") or "email",
        "avatar_url": doc.get("avatar_url"),
        "created_at": doc.get("created_at"),
        "last_login_at": doc.get("last_login_at"),
        "login_count": doc.get("login_count") or 0,
        "is_admin": _is_admin_email(email) or bool(doc.get("is_admin")),
    }


def _auth_response(user_doc: dict, token: str) -> dict:
    profile = _serialize_user(user_doc or {})
    return {
        "access_token": token,
        "status": "success",
        "username": profile.get("username") or "",
        "email": profile.get("email") or "",
        "auth_provider": profile.get("auth_provider") or "email",
        "avatar_url": profile.get("avatar_url"),
        "is_admin": profile.get("is_admin", False),
    }


@app.get("/me")
async def get_profile(user=Depends(get_current_user)):
    email = _normalize_email(user.get("email"))
    existing = await db["users"].find_one(_email_lookup_filter(email))
    if not existing:
        raise HTTPException(status_code=404, detail="User profile not found.")
    return _serialize_user(existing)


@app.put("/me")
async def update_profile(payload: ProfileUpdate, user=Depends(get_current_user)):
    email = _normalize_email(user.get("email"))
    existing = await db["users"].find_one(_email_lookup_filter(email))
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

    await db["users"].update_one({"_id": existing["_id"]}, {"$set": updates})
    refreshed = await db["users"].find_one({"_id": existing["_id"]})
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
        email = user.email.strip().lower()
        existing_user = await db["users"].find_one(_email_lookup_filter(email))

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

        token = create_access_token({"email": email})

        await record_auth_event(
            email,
            "login",
            existing_user.get("auth_provider") or "email",
            existing_user.get("username") or "",
        )
        existing_user = await db["users"].find_one(_email_lookup_filter(email))
        return _auth_response(existing_user, token)
    except HTTPException:
        raise
    except Exception as e:
        print("LOGIN DB ERROR:", e)
        raise HTTPException(
            status_code=503,
            detail="Database connection failed. Check MONGODB_URI/network and try again."
        )


@app.get("/admin/overview")
async def admin_overview(_admin=Depends(require_admin)):
    total_users = await db["users"].count_documents({})
    total_chats = await db.chat_history.count_documents({})
    google_users = await db["users"].count_documents({"auth_provider": "google"})
    email_users = await db["users"].count_documents({
        "$or": [
            {"auth_provider": {"$exists": False}},
            {"auth_provider": "email"},
        ]
    })

    t_start, t_end = _utc_day_bounds(0)
    y_start, y_end = _utc_day_bounds(1)

    registrations_today = await db["users"].count_documents({"created_at": {"$gte": t_start, "$lt": t_end}})
    registrations_yesterday = await db["users"].count_documents({"created_at": {"$gte": y_start, "$lt": y_end}})
    logins_today = await db.login_events.count_documents({"created_at": {"$gte": t_start, "$lt": t_end}})
    logins_yesterday = await db.login_events.count_documents({"created_at": {"$gte": y_start, "$lt": y_end}})

    all_chat_emails = await db.chat_history.distinct("user_email")
    guest_sessions = sum(1 for e in all_chat_emails if e and str(e).startswith("guest:"))
    collection_names = await db.list_collection_names()
    rag_chunks = await db.rag_chunks.count_documents({}) if "rag_chunks" in collection_names else 0
    vector_index_active = False
    try:
        indexes = await db.chat_history.list_search_indexes().to_list(length=10)
        vector_index_active = any((idx.get("status") or "").lower() == "ready" for idx in indexes)
    except Exception:
        vector_index_active = False

    return {
        "total_users": total_users,
        "total_chats": total_chats,
        "google_users": google_users,
        "email_users": email_users,
        "registrations_today": registrations_today,
        "registrations_yesterday": registrations_yesterday,
        "logins_today": logins_today,
        "logins_yesterday": logins_yesterday,
        "database": MONGODB_DB_NAME,
        "guest_sessions": guest_sessions,
        "login_events_total": await db.login_events.count_documents({}),
        "rag_chunks": rag_chunks,
        "vector_search_enabled": vector_index_active,
        "mongodb_connected": True,
    }


@app.get("/admin/users")
async def admin_users(
    email: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    _admin=Depends(require_admin),
):
    try:
        query = {}
        if email and email.strip():
            query["email"] = {"$regex": re.escape(email.strip()), "$options": "i"}
        activity_filter = _admin_user_activity_filter(period, since, until)
        if activity_filter:
            if query:
                query = {"$and": [query, activity_filter]}
            else:
                query = activity_filter

        user_docs = await db["users"].find(query).sort(
            [("created_at", -1), ("last_login_at", -1), ("_id", -1)]
        ).to_list(length=2000)
        chat_counts = await _chat_counts_by_email()

        rows = []
        for doc in user_docs:
            try:
                profile = _serialize_user(doc)
                email_key = _normalize_email(profile.get("email"))
                profile["chat_count"] = chat_counts.get(email_key, 0)
                profile["created_at"] = _format_admin_datetime(profile.get("created_at"))
                profile["last_login_at"] = _format_admin_datetime(profile.get("last_login_at"))
                rows.append(profile)
            except Exception as exc:
                print(f"ADMIN USER SERIALIZE ERROR ({doc.get('email')}):", exc)

        return {"users": rows, "count": len(rows), "database": MONGODB_DB_NAME}
    except HTTPException:
        raise
    except Exception as exc:
        print("ADMIN USERS ERROR:", exc)
        raise HTTPException(status_code=500, detail=f"Could not load users from database: {exc}")


@app.get("/admin/logins")
async def admin_logins(
    email: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 200,
    _admin=Depends(require_admin),
):
    safe_limit = max(1, min(limit, 500))
    query = {}
    if email and email.strip():
        query["email"] = {"$regex": re.escape(email.strip()), "$options": "i"}
    query.update(_admin_date_filter(period, since, until, "created_at"))

    rows = []
    cursor = db.login_events.find(query).sort("created_at", -1).limit(safe_limit)
    async for doc in cursor:
        rows.append({
            "_id": str(doc.get("_id")),
            "email": doc.get("email") or "",
            "username": doc.get("username") or "",
            "event": doc.get("event") or "",
            "auth_provider": doc.get("auth_provider") or "",
            "created_at": _format_admin_datetime(doc.get("created_at")),
        })
    return {"logins": rows, "count": len(rows)}


@app.get("/admin/activity")
async def admin_activity(
    email: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 300,
    _admin=Depends(require_admin),
):
    """Every chat identity in MongoDB — registered users, guests, and chat-only emails."""
    rows = await _chat_activity_rows(limit=max(1, min(limit, 1000)))
    if email and email.strip():
        needle = email.strip().lower()
        rows = [r for r in rows if needle in (r.get("identity") or "").lower() or needle in (r.get("email") or "").lower()]
    date_filter = _admin_date_filter(period, since, until, "last_seen")
    if date_filter.get("last_seen"):
        rng = date_filter["last_seen"]
        filtered = []
        for row in rows:
            last_seen = row.get("last_seen")
            if not last_seen:
                continue
            try:
                dt = datetime.fromisoformat(str(last_seen).replace("Z", ""))
            except ValueError:
                continue
            if rng.get("$gte") and dt < rng["$gte"]:
                continue
            if rng.get("$lt") and dt >= rng["$lt"]:
                continue
            filtered.append(row)
        rows = filtered
    return {"activity": rows, "count": len(rows), "database": MONGODB_DB_NAME}


@app.delete("/admin/users")
async def admin_delete_user(email: str, admin=Depends(require_admin)):
    target = (email or "").strip().lower()
    if not target:
        raise HTTPException(status_code=400, detail="Email is required.")

    admin_email = (admin.get("email") or "").strip().lower()
    if target == admin_email:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")

    if _is_admin_email(target):
        raise HTTPException(status_code=400, detail="Admin accounts cannot be deleted.")

    existing = await db["users"].find_one({
        "email": {"$regex": f"^{re.escape(target)}$", "$options": "i"},
    })
    if not existing:
        raise HTTPException(status_code=404, detail="User not found.")

    actual_email = existing.get("email") or target
    await db["users"].delete_one({"email": actual_email})
    await db.chat_history.delete_many({
        "user_email": {"$regex": f"^{re.escape(actual_email)}$", "$options": "i"},
    })

    return {
        "status": "success",
        "email": actual_email,
        "message": "User and their chat history were deleted.",
    }


@app.get("/admin/chats")
async def admin_chats(
    limit: int = 100,
    email: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    _admin=Depends(require_admin),
):
    safe_limit = max(1, min(limit, 500))
    query = {}
    if email and email.strip():
        query["user_email"] = {"$regex": re.escape(email.strip()), "$options": "i"}
    query.update(_admin_date_filter(period, since, until, "timestamp"))

    cursor = db.chat_history.find(query).sort("timestamp", -1).limit(safe_limit)
    chats = []
    async for doc in cursor:
        chats.append({
            "_id": str(doc.get("_id")),
            "user_email": doc.get("user_email") or "",
            "user_query": doc.get("user_query") or "",
            "ai_response": doc.get("ai_response") or "",
            "session_id": doc.get("session_id") or "",
            "timestamp": doc.get("timestamp").isoformat() if doc.get("timestamp") else None,
        })
    return {"chats": chats}

