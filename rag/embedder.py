from sentence_transformers import SentenceTransformer

model = None
model_failed = False

def get_embedding(text):
    global model, model_failed

    if model_failed:
        return None

    if model is None:
        try:
            model = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as e:
            model_failed = True
            print(f"EMBEDDER INIT ERROR: {e}")
            return None

    try:
        return model.encode(text).tolist()
    except Exception as e:
        print(f"EMBEDDER ENCODE ERROR: {e}")
        return None
