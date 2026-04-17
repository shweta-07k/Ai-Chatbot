# Production RAG Add-on

This project now includes a production-oriented RAG layer with:
- PDF ingestion
- Mongo-backed vector chunk store
- Redis caching for retrieval results
- Source attribution in chat UI
- Docker deployment (API + Redis + Frontend)

## Environment

Set these in `.env` (optional overrides):

- `RAG_CHUNKS_COLLECTION=rag_chunks`
- `RAG_DOCS_COLLECTION=rag_documents`
- `RAG_CHUNK_SIZE=900`
- `RAG_CHUNK_OVERLAP=180`
- `RAG_TOP_K=5`
- `RAG_MAX_CANDIDATES=5000`
- `RAG_USE_REDIS=true`
- `RAG_CACHE_TTL_SEC=300`
- `REDIS_URL=redis://redis:6379/0`

## New API Endpoints

1. `POST /rag/ingest-pdf`
- Multipart form-data:
  - `file`: PDF file
  - `source_label` (optional)
- Requires Authorization Bearer token

2. `POST /rag/ingest-file`
- Multipart form-data:
  - `file`: PDF/PNG/JPG/JPEG
  - `source_label` (optional)
- Requires Authorization Bearer token

3. `POST /rag/query`
- JSON body:
```json
{
  "query": "What does policy X say?",
  "top_k": 5
}
```
- Requires Authorization Bearer token
- Returns `answer` + structured `sources`

## Chat Integration

`/chat` now also checks ingested PDF chunks and appends matching context.
Responses include `sources` so UI shows source attribution under AI messages.

## Docker

Run full stack:

```bash
docker compose up --build
```

Services:
- API: `http://localhost:8000`
- Frontend: `http://localhost:3000`
- Redis: `localhost:6379`

Note: MongoDB remains external (from `MONGODB_URI` in `.env`).
