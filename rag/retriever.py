from db.vector_store import search_vectors

def retrieve(query_embedding, top_k=3):
    results = search_vectors(query_embedding, top_k)
    return results