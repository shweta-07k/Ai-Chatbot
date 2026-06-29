import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from neo4j import AsyncDriver, AsyncGraphDatabase

from .config import settings
from .retrieve import retrieve_chunks_with_cache
from rag.retriever import retrieve as local_vector_retrieve

neo4j_driver: Optional[AsyncDriver] = None
neo4j_driver_config: Optional[tuple[str, str, str]] = None


def _neo4j_enabled() -> bool:
    load_dotenv(override=True)
    return str(os.getenv("NEO4J_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}

def get_neo4j_driver() -> Optional[AsyncDriver]:
    """Return a lazily initialized Neo4j async driver.

    The driver is only created when valid environment variables are present.
    If Neo4j is not configured, functions simply return None and the app
    continues using the existing vector retrieval flow.
    """
    global neo4j_driver, neo4j_driver_config

    if not _neo4j_enabled():
        return None

    # Uvicorn's reload watcher does not reliably restart when only .env changes.
    # Reload Neo4j settings here so a newly generated Aura password is picked up.
    load_dotenv(override=True)

    uri = (os.getenv("NEO4J_URI") or settings.neo4j_uri or "").strip()
    user = (os.getenv("NEO4J_USER") or settings.neo4j_user or "neo4j").strip()
    password = (os.getenv("NEO4J_PASSWORD") or settings.neo4j_password or "").strip()
    if not uri or not user or not password:
        return None

    current_config = (uri, user, password)
    if neo4j_driver is not None and neo4j_driver_config == current_config:
        return neo4j_driver

    try:
        neo4j_driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        neo4j_driver_config = current_config
        return neo4j_driver
    except Exception as exc:
        print("NEO4J DRIVER INIT ERROR:", exc)
        return None

async def verify_neo4j_connection() -> bool:
    """Explicitly tests the driver credentials against the running database instance."""
    if not _neo4j_enabled():
        print("Neo4j is disabled (NEO4J_ENABLED=false). Using MongoDB vector RAG only.")
        return False

    driver = get_neo4j_driver()
    if not driver:
        print("Neo4j configuration missing or incomplete. Skipping connection test.")
        return False
        
    try:
        await driver.verify_connectivity()
        print("GraphRAG database connected successfully via Async Bolt.")
        return True
    except Exception as e:
        err = str(e)
        if "10061" in err or "Connect call failed" in err or "Failed to establish connection" in err:
            print(
                "Neo4j is configured but not running on "
                f"{os.getenv('NEO4J_URI', settings.neo4j_uri)}. "
                "Start it with: docker compose up -d neo4j "
                "(or set NEO4J_ENABLED=false to skip GraphRAG)."
            )
        else:
            print(f"Neo4j auth/connection error: {e}")
        return False


def extract_entities(text: str, max_entities: int = 8) -> List[str]:
    """Extract simple entity candidates from text for graph matching."""
    if not text:
        return []
    cleaned = re.sub(r"\s+", " ", text.strip())

    # 1) Prefer multi-word TitleCase / Proper Noun matches (good for document headers)
    pattern = r"\b([A-Z][A-Za-z0-9\-]*(?:\s+[A-Z][A-Za-z0-9\-]*){0,3})\b"
    found: List[str] = []
    for match in re.finditer(pattern, cleaned):
        candidate = match.group(1).strip()
        if len(candidate) < 3:
            continue
        lower = candidate.lower()
        if lower in {"the", "and", "for", "with", "from", "that", "this", "your"}:
            continue
        if candidate not in found:
            found.append(candidate)
        if len(found) >= max_entities:
            break

    # 2) Fallback: if no TitleCase entities found, pick top content words (stopword-filtered)
    if not found:
        # lightweight stopword list to filter out common words
        stopwords = {
            "the",
            "and",
            "for",
            "with",
            "from",
            "that",
            "this",
            "your",
            "are",
            "was",
            "were",
            "have",
            "has",
            "had",
            "not",
            "but",
            "what",
            "which",
            "when",
            "where",
            "how",
            "why",
            "can",
            "could",
            "should",
            "would",
            "may",
        }

        tokens = re.findall(r"[A-Za-z0-9\-]{3,}", cleaned)
        freq: Dict[str, int] = {}
        for t in tokens:
            tl = t.lower()
            if tl in stopwords:
                continue
            freq[tl] = freq.get(tl, 0) + 1

        # sort by frequency then length, return original-cased tokens where possible
        sorted_tokens = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0])))
        for tok, _ in sorted_tokens[:max_entities]:
            # try to find the original-cased form in text; fallback to token
            orig_match = re.search(rf"\b({re.escape(tok)})\b", cleaned, flags=re.IGNORECASE)
            if orig_match:
                candidate = orig_match.group(1).strip()
            else:
                candidate = tok
            if candidate not in found:
                found.append(candidate)
            if len(found) >= max_entities:
                break

    return found


def _chunk_key(chunk: Dict[str, Any]) -> str:
    return (
        chunk.get("chunk_hash")
        or f"{chunk.get('source','')}|{chunk.get('page')}|{chunk.get('chunk_index')}|{chunk.get('text','')[:80]}"
    )


async def create_document_graph(
    driver: AsyncDriver,
    doc_id: str,
    filename: str,
    chunk_docs: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Create graph nodes and relationships for an ingested document."""
    if driver is None:
        return

    metadata = metadata or {}
    uploaded_by = metadata.get("uploaded_by", "unknown")
    source_type = metadata.get("source_type", "document")
    created_at = metadata.get("created_at")

    async with driver.session() as session:
        await session.execute_write(
            _create_document_graph_tx,
            doc_id,
            filename,
            source_type,
            uploaded_by,
            created_at,
            chunk_docs,
        )


def _create_document_graph_tx(
    tx,
    doc_id: str,
    filename: str,
    source_type: str,
    uploaded_by: str,
    created_at: Any,
    chunk_docs: List[Dict[str, Any]],
) -> None:
    tx.run(
        """
        MERGE (doc:Document {doc_id: $doc_id})
        SET doc.filename = $filename,
            doc.source_type = $source_type,
            doc.uploaded_by = $uploaded_by,
            doc.created_at = $created_at
        """,
        doc_id=doc_id,
        filename=filename,
        source_type=source_type,
        uploaded_by=uploaded_by,
        created_at=str(created_at) if created_at is not None else None,
    )

    for chunk in chunk_docs:
        chunk_hash = chunk.get("chunk_hash")
        text = chunk.get("text", "")
        source = chunk.get("source", "unknown")
        page = chunk.get("page")
        chunk_index = chunk.get("chunk_index")
        entities = extract_entities(text)

        tx.run(
            """
            MERGE (chunk:Chunk {chunk_hash: $chunk_hash})
            SET chunk.doc_id = $doc_id,
                chunk.source = $source,
                chunk.page = $page,
                chunk.chunk_index = $chunk_index,
                chunk.text = $text
            """,
            chunk_hash=chunk_hash,
            doc_id=doc_id,
            source=source,
            page=page,
            chunk_index=chunk_index,
            text=text,
        )

        tx.run(
            """
            MATCH (doc:Document {doc_id: $doc_id})
            MATCH (chunk:Chunk {chunk_hash: $chunk_hash})
            MERGE (doc)-[:HAS_CHUNK]->(chunk)
            """,
            doc_id=doc_id,
            chunk_hash=chunk_hash,
        )

        for entity_name in entities:
            tx.run(
                """
                MERGE (ent:Entity {name: $entity_name})
                ON CREATE SET ent.first_seen = timestamp()
                WITH ent
                MATCH (chunk:Chunk {chunk_hash: $chunk_hash})
                MERGE (chunk)-[:MENTIONS]->(ent)
                """,
                entity_name=entity_name,
                chunk_hash=chunk_hash,
            )


async def graph_search(
    driver: AsyncDriver,
    query: str,
    max_results: int = 4,
) -> List[Dict[str, Any]]:
    if driver is None or not query:
        return []

    entities = extract_entities(query, max_entities=6)
    if not entities:
        return []

    lower_entities = [entity.lower() for entity in entities]
    async with driver.session() as session:
        return await session.execute_read(_graph_search_tx, lower_entities, max_results)


def _graph_search_tx(tx, entities: List[str], max_results: int) -> List[Dict[str, Any]]:
    result = tx.run(
        """
        UNWIND $entities AS entity_name
        MATCH (e:Entity)-[:MENTIONS]->(chunk:Chunk)
        WHERE toLower(e.name) = entity_name
        RETURN DISTINCT chunk.doc_id AS doc_id,
                        chunk.source AS source,
                        chunk.page AS page,
                        chunk.chunk_index AS chunk_index,
                        chunk.text AS text,
                        chunk.chunk_hash AS chunk_hash,
                        collect(DISTINCT e.name) AS matched_entities
        ORDER BY chunk.page, chunk.chunk_index
        LIMIT $limit
        """,
        entities=entities,
        limit=max_results,
    )
    return [record.data() for record in result]


async def hybrid_retrieve(
    db,
    query: str,
    query_embedding: Optional[List[float]],
    embed_fn,
    top_k: int = 4,
    owner_email: Optional[str] = None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen = set()

    # 1) Local vector search from the existing in-memory retrieval path.
    if query_embedding is not None:
        local_docs = local_vector_retrieve(query_embedding, top_k=top_k)
        for doc in local_docs:
            key = _chunk_key(doc)
            if key not in seen:
                seen.add(key)
                results.append({**doc, "score": doc.get("score", 0.0), "source_type": "local_vector"})

    # 2) Production vector retrieval from MongoDB + cache.
    if owner_email:
        try:
            prod_docs = await retrieve_chunks_with_cache(
                db=db,
                query=query,
                embed_fn=embed_fn,
                top_k=top_k,
                owner_email=owner_email,
            )
            for doc in prod_docs:
                key = _chunk_key(doc)
                if key not in seen:
                    seen.add(key)
                    results.append({**doc, "source_type": "mongo_vector"})
        except Exception as exc:
            print("HYBRID PROD RETRIEVE ERROR:", exc)

    # 3) Graph search on Neo4j.
    driver = get_neo4j_driver()
    if driver is not None:
        try:
            graph_docs = await graph_search(driver, query, max_results=top_k)
            for doc in graph_docs:
                key = _chunk_key(doc)
                if key not in seen:
                    seen.add(key)
                    results.append({**doc, "score": 0.9, "source_type": "neo4j_graph"})
        except Exception as exc:
            print("HYBRID GRAPH SEARCH ERROR:", exc)

    return results
