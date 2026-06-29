FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=300
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

# faiss-cpu needs libgomp on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-render.txt /app/requirements-render.txt
COPY bin/render-build.sh /app/bin/render-build.sh
RUN chmod +x /app/bin/render-build.sh && bash /app/bin/render-build.sh

COPY main.py app.py /app/
COPY rag_prod/ /app/rag_prod/
COPY rag/ /app/rag/
COPY db/ /app/db/

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
