import os
import re
import requests
import json
from datetime import datetime,timedelta
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from html import unescape
from fastapi import FastAPI, UploadFile, File, Form
from jose import jwt
from jose import JWTError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.ai.inference import ChatCompletionsClient
from azure.core.credentials import AzureKeyCredential
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
from bson import ObjectId
from fastapi import HTTPException
import bcrypt
from pydantic import BaseModel, EmailStr,Field
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from rag.retriever import retrieve
from rag_prod.ingest import ingest_pdf_to_mongo, ingest_image_to_mongo
from rag_prod.retrieve import retrieve_chunks_with_cache
from rag_prod.config import settings as rag_settings




SECRET_KEY = "mysecret123"
ALGORITHM = "HS256"
security = HTTPBearer()
load_dotenv()
app = FastAPI()

# 1. Database & Local AI Setup
# Lazy-load embedder so app can still start if HF/model download is unavailable
embed_model = None
embed_model_failed = False
db_client = AsyncIOMotorClient(os.getenv("MONGODB_URI"))
db = db_client.ai_project

http_session = requests.Session()
http_session.trust_env = False  # Ignore broken proxy env vars that can block live API calls

def get_embed_model():
    global embed_model, embed_model_failed
    if embed_model_failed:
        return None
    if embed_model is None:
        try:
            embed_model = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as e:
            embed_model_failed = True
            print(f"EMBED MODEL LOAD ERROR: {e}")
            return None
    return embed_model

def build_embedding(text: str):
    model = get_embed_model()
    if model is None:
        return None
    try:
        return model.encode(text).tolist()
    except Exception as e:
        print(f"EMBED ERROR: {e}")
        return None

# 2. CORS (stronger, explicit for React frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
    # expose_headers=["*"],
)

# Real-time data fetching functions
def get_weather(location: str):
    """Fetch current weather; fallback to MET Norway if Open-Meteo is unavailable."""
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={location}&count=1&language=en&format=json"
        geo_resp = http_session.get(geo_url, timeout=6)
        geo_resp.raise_for_status()
        geo_data = geo_resp.json()

        if not geo_data.get("results"):
            return f"Could not find location: {location}"

        lat = geo_data["results"][0]["latitude"]
        lon = geo_data["results"][0]["longitude"]
        name = geo_data["results"][0]["name"]

        try:
            weather_url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&current=temperature_2m,weather_code,wind_speed_10m&temperature_unit=celsius"
            )
            weather_resp = http_session.get(weather_url, timeout=8)
            weather_resp.raise_for_status()
            weather_data = weather_resp.json()
            current = weather_data.get("current", {})
            temp = current.get("temperature_2m")
            wind = current.get("wind_speed_10m")
            if temp is not None and wind is not None:
                return f"Weather in {name}: {temp} C, Wind: {wind} km/h (source: Open-Meteo)"
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
    msg = (message or "").strip()
    if not msg:
        return None

    # Prefer phrases after common prepositions.
    match = re.search(
        r"\b(?:in|at|for|near)\s+([A-Za-z][A-Za-z\s,-]{1,60})",
        msg,
        re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip(" ,.?!")
        # Remove trailing time words often attached to queries.
        candidate = re.sub(
            r"\b(today|tomorrow|now|currently|right now|please)\b.*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip(" ,.?!")
        if candidate:
            return candidate

    # Fallback: if user only typed a city-like token.
    cleaned = re.sub(r"[^A-Za-z\s,-]", " ", msg).strip()
    tokens = [t for t in re.split(r"\s+", cleaned) if t]
    stop = {
        "what", "whats", "is", "the", "weather", "temperature", "forecast",
        "rain", "climate", "in", "at", "for", "near", "today", "now", "current",
    }
    non_stop = [t for t in tokens if t.lower() not in stop]
    if non_stop:
        return " ".join(non_stop[:3]).strip(" ,.?!")

    return None

def web_search(query: str):
    """Real-time web/news lookup with Brave (if key exists) + no-key fallbacks."""
    try:
        safe_query = quote_plus(query)
        brave_key = os.getenv("BRAVE_API_KEY")

        # 1) Brave Search (preferred when key exists)
        if brave_key:
            url = f"https://api.search.brave.com/res/v1/web/search?q={safe_query}&count=5"
            headers = {"Authorization": f"Bearer {brave_key}"}
            resp = http_session.get(url, headers=headers, timeout=7)
            resp.raise_for_status()
            payload = resp.json()
            web_data = payload.get("web", {})
            results = web_data.get("results", []) if isinstance(web_data, dict) else []
            if results:
                summary_parts = []
                for r in results[:3]:
                    title = r.get("title", "Untitled")
                    description = r.get("description", "")
                    result_url = r.get("url", "")
                    summary_parts.append(f"{title}: {description} ({result_url})")
                return "Recent search results: " + " | ".join(summary_parts)

        # 2) Google News RSS fallback (no key required)
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

        # 3) DuckDuckGo instant answer fallback (no key required)
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
    email: str
    session_id: str = None  # Optional session for grouping conversations


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
        print("TOKEN:", token)
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        print("PAYLOAD:", payload)

        email: str = payload.get("email")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")

        return payload

    except JWTError as e:
        print("JWT ERROR:", str(e))
        raise HTTPException(status_code=401, detail="Invalid or expired token")




@app.post("/chat")
async def chat_with_ai(request: ChatRequest):
    try:
        token = os.getenv("GITHUB_TOKEN")
        ai_client = ChatCompletionsClient(
            endpoint="https://models.inference.ai.azure.com",
            credential=AzureKeyCredential(token),
        )

        # Fetch last 5 messages from user for context
        recent_history = await db.chat_history.find(
            {"user_email": request.email}
        ).sort("timestamp", -1).limit(5).to_list(length=5)
        
        # Reverse to get chronological order (oldest to newest)
        recent_history.reverse()
        
        # Detect real-time data needs
        real_time_context = ""
        message_lower = request.message.lower()
        direct_reply = None
        direct_sources = []
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
            search_data = web_search(request.message)
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
            direct_sources = ["NSE", "BSE", "Google News RSS/Brave/DDG"]

        elif is_home_market_query and any(x in message_lower for x in ["india", "indian"]):
            home_data = get_india_home_market_snapshot()
            search_data = web_search(request.message)
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
            direct_sources = ["Numbeo", "Google News RSS/Brave/DDG"]

        elif any(word in message_lower for word in ["weather", "temperature", "rain", "forecast", "climate"]):
            location = extract_weather_location(request.message)
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
        
        elif any(word in message_lower for word in ["current", "today", "now", "latest", "recent"]) and not real_time_context:
            search_data = web_search(request.message)
            real_time_context = f"\n[{get_current_info()}]\n[Real-time Search Data: {search_data}]"

        # General live-information fallback for news/market/sports or explicit search intent
        elif any(
            word in message_lower
            for word in ["news", "price", "stock", "crypto", "score", "match", "headlines", "search", "update"]
        ) and not real_time_context:
            search_data = web_search(request.message)
            real_time_context = f"\n[Real-time Search Data: {search_data}]"
        
        # Build conversation context
        system_prompt = f"""You are a professional AI Data Infrastructure Assistant with access to real-time information and retrieved documents.

Use the provided context (if any) to answer accurately. Do NOT hallucinate.
If real-time context is available below, prioritize it over stale model memory.
For real-time market questions, give exact numeric values from context, include "As of" timestamp, and cite source text present in the context.

{real_time_context}
"""
        messages = [
            {"role": "system", "content": system_prompt}
        ]
        
        # Add previous messages to context
        for chat in recent_history:
            messages.append({"role": "user", "content": chat["user_query"]})
            messages.append({"role": "assistant", "content": chat["ai_response"]})
        
        # Add current user message
        # ===== RAG INTEGRATION START =====
        # Step 1: Embed query
        query_embedding = build_embedding(request.message)
        # Step 2: Retrieve relevant docs (from your vector DB)
        docs = []
        if query_embedding is not None:
            try:
                docs = retrieve(query_embedding)
            except Exception as e:
                print("RETRIEVE ERROR:", e)
        # Step 3: Build context
        rag_context = ""
        sources = []
        if docs:
            rag_context = "\n\nRelevant Information:\n"
            for doc in docs:
                rag_context += doc["text"] + "\n"
                sources.append(doc.get("source", "unknown"))
        # Step 4: Add RAG context to user message
        enhanced_query = request.message + rag_context
        messages.append({"role": "user", "content": enhanced_query})
        # ===== RAG INTEGRATION END =====

        # ===== PRODUCTION RAG (PDF + Mongo + cache) =====
        prod_docs = []
        if query_embedding is not None:
            try:
                prod_docs = await retrieve_chunks_with_cache(
                    db=db,
                    query=request.message,
                    embed_fn=build_embedding,
                    top_k=4,
                    owner_email=request.email,
                )
            except Exception as e:
                print("PROD RAG RETRIEVE ERROR:", e)

        if prod_docs:
            prod_context = "\n\nPDF Knowledge Base Context:\n"
            for d in prod_docs:
                snippet = (d.get("text") or "")[:700]
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
            messages[-1]["content"] = messages[-1]["content"] + prod_context

        # Get AI Response with full context
        if direct_reply:
            ai_reply = direct_reply
            sources.extend([{"source": s, "page": None, "score": None} for s in direct_sources])
        else:
            response = ai_client.complete(
                messages=messages,
                model="gpt-4o"
            )
            if not response:
                ai_reply = "AI service unavailable."

            elif not hasattr(response, "choices") or len(response.choices) == 0:
                print("EMPTY RESPONSE:", response)
                ai_reply = "No response generated."

            elif not hasattr(response.choices[0], "message"):
                ai_reply = "Invalid response format."

            else:
                ai_reply = response.choices[0].message.content or "Empty response"

        # Save to MongoDB with vector embedding
        vector = build_embedding(request.message)
        
        chat_doc = {
             "user_email": request.email,
             "user_query": request.message,
             "ai_response": ai_reply,
             "embedding": vector or [],
             "session_id": request.session_id,
             "timestamp": datetime.utcnow()
        }
        await db.chat_history.insert_one(chat_doc)

        return {"reply": ai_reply, "sources": sources}
        
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


@app.get("/ping")
async def ping():
    return {"ok": True}


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
            allowed = (".pdf", ".png", ".jpg", ".jpeg")
            if not lower.endswith(allowed):
                raise HTTPException(status_code=400, detail="Supported files: pdf, png, jpg, jpeg")

            payload = await file.read()
            if not payload:
                raise HTTPException(status_code=400, detail="Empty file.")

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
            else:
                stats = await ingest_image_to_mongo(
                    db=db,
                    filename=filename,
                    image_bytes=payload,
                    embed_fn=build_embedding,
                    metadata=metadata,
                )

            return {"status": "success", "ingestion": stats}
        except HTTPException:
            raise
        except Exception as e:
            print("RAG INGEST FILE ERROR:", e)
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

        token = os.getenv("GITHUB_TOKEN")
        ai_client = ChatCompletionsClient(
            endpoint="https://models.inference.ai.azure.com",
            credential=AzureKeyCredential(token),
        )
        prompt = (
            "Answer the user only with facts supported by the context. "
            "If information is missing, say so clearly. Include a short 'Sources' list."
        )
        response = ai_client.complete(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Question: {query}\n\nContext:\n{context}"},
            ],
        )
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
    # 1. Check if email already exists in MongoDB
    existing_user = await db["users"].find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="User email already exists.")

    # 2. Hash the password before saving
    hashed_password = get_password_hash(user.password)

    new_user = {
        "username": user.username,
        "email": user.email,
        "password": hashed_password  # Store ONLY the hash
    }
    
    await db["users"].insert_one(new_user)
    return {"status": "success"}

class UserLogin(BaseModel):
    email: str
    password: str


@app.post("/login")
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


