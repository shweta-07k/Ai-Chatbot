from rag.embedder import get_embedding
from rag.retriever import retrieve
from rag.generator import generate_answer

def rag_pipeline(query):
    # Step 1: embed query
    query_embedding = get_embedding(query)

    # Step 2: retrieve docs
    docs = retrieve(query_embedding)

    # combine context
    context = " ".join([doc["text"] for doc in docs])

    # Step 3: generate answer
    answer = generate_answer(query, context)

    # Step 4: return with sources
    sources = [doc["source"] for doc in docs]

    return {
        "answer": answer,
        "sources": sources
    }