from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()

def _as_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RagSettings:
    chunks_collection: str = os.getenv("RAG_CHUNKS_COLLECTION", "rag_chunks")
    docs_collection: str = os.getenv("RAG_DOCS_COLLECTION", "rag_documents")
    chunk_size: int = int(os.getenv("RAG_CHUNK_SIZE", "900"))
    chunk_overlap: int = int(os.getenv("RAG_CHUNK_OVERLAP", "180"))
    top_k_default: int = int(os.getenv("RAG_TOP_K", "5"))
    max_candidates: int = int(os.getenv("RAG_MAX_CANDIDATES", "5000"))
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    cache_ttl_sec: int = int(os.getenv("RAG_CACHE_TTL_SEC", "300"))
    use_redis: bool = _as_bool(os.getenv("RAG_USE_REDIS", "true"), True)
    neo4j_enabled: bool = _as_bool(os.getenv("NEO4J_ENABLED", "true"), True)
    neo4j_uri: str = os.getenv("NEO4J_URI", "")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")


settings = RagSettings()
