import faiss
import numpy as np

dimension = 384
index = faiss.IndexFlatL2(dimension)

documents = []

def add_vector(vector, text, source):
    global documents
    index.add(np.array([vector]).astype('float32'))
    documents.append({
        "text": text,
        "source": source
    })

def search_vectors(query_vector, top_k=3):
    if index.ntotal == 0:
        return []   # ✅ FIX: no crash if empty DB

    try:
        D, I = index.search(np.array([query_vector]).astype('float32'), top_k)
    except Exception as e:
        print("FAISS ERROR:", e)
        return []

    results = []

    if len(I) == 0:
        return results

    for i in I[0]:
        if i < len(documents):
            results.append(documents[i])

    return results