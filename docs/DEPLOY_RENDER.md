# Deploy Nova AI Chatbot on Render

This guide deploys **two Render services** from your GitHub repo:

| Service | Type | URL (example) |
|---------|------|----------------|
| `nova-ai-api` | Web (Docker) | `https://nova-ai-api.onrender.com` |
| `nova-ai-frontend` | Static site | `https://nova-ai-frontend.onrender.com` |

**Share the frontend URL with friends** — that is your live Nova AI website.

---

## 1. Prerequisites

1. [Render account](https://render.com/) (free tier works)
2. GitHub repo: [shweta-07k/Ai-Chatbot](https://github.com/shweta-07k/Ai-Chatbot)
3. [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) cluster (free M0)
4. GitHub Models token in `GITHUB_TOKEN`
5. (Optional) Google OAuth client for Sign in with Google

---

## 2. MongoDB Atlas

1. Create a free cluster.
2. Database Access → add a user with password.
3. Network Access → **Allow access from anywhere** (`0.0.0.0/0`) so Render can connect.
4. Copy connection string → set as `MONGODB_URI`.

---

## 3. Deploy with Blueprint (recommended)

1. Open [Render Dashboard](https://dashboard.render.com/)
2. **New** → **Blueprint**
3. Connect GitHub → select **Ai-Chatbot**
4. Render reads `render.yaml` and creates both services.
5. When prompted, set **secret** env vars:
   - `MONGODB_URI`
   - `GITHUB_TOKEN`
   - `GOOGLE_CLIENT_ID` (optional)
6. Click **Apply** and wait for builds (API first build may take 10–15 minutes).

### If `nova-ai-api` failed with Docker

The repo now uses **Python native runtime** (not Docker) for the API — more reliable on Render free tier.

**Fix an existing failed API service:**

1. Render Dashboard → **nova-ai-api** → **Settings**
2. Change **Environment** from Docker to **Python 3**
3. Set:
   - **Build command:** `bash bin/render-build.sh`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Health check path:** `/health`
4. **Manual Deploy** → Deploy latest commit

Or delete the API service and re-run the Blueprint from the latest `main` branch.

---

## 4. After deploy — your live links

Render shows URLs on each service page:

- **Website for friends:** `https://nova-ai-frontend.onrender.com`
- **API (backend):** `https://nova-ai-api.onrender.com`
- **Health check:** `https://nova-ai-api.onrender.com/health`

If service names differ, use the URLs from your Render dashboard.

---

## 5. Google Sign-In (optional)

In [Google Cloud Console](https://console.cloud.google.com/) → OAuth client:

**Authorized JavaScript origins:**

- `https://nova-ai-frontend.onrender.com`
- `http://localhost:3000`

**Authorized redirect URIs:** (if required by your setup)

- `https://nova-ai-frontend.onrender.com`

Set `GOOGLE_CLIENT_ID` on the **API** service in Render → Environment.

Redeploy the **frontend** after changing env vars (static site bakes `REACT_APP_*` at build time).

---

## 6. Manual deploy (without Blueprint)

### API (Web Service)

- **Root directory:** `.` (repo root)
- **Environment:** Python 3 (not Docker)
- **Build command:** `bash bin/render-build.sh`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Health check path:** `/health`
- **Env vars:** see `.env.example`

### Frontend (Static Site)

- **Root directory:** `ai-frontend`
- **Build command:** `npm install && npm run build`
- **Publish directory:** `build`
- **Env var:** `REACT_APP_API_URL` = `https://nova-ai-api.onrender.com` (your API URL)

---

## 7. Free tier notes

- Services **sleep after ~15 min idle**; first request may take 30–60 seconds to wake.
- API cold start may download the embedding model on first chat (set `ALLOW_MODEL_DOWNLOAD=1`).
- Neo4j and Redis are **disabled** in `render.yaml` by default (`NEO4J_ENABLED=false`, `RAG_USE_REDIS=false`). MongoDB RAG still works.

---

## 8. Troubleshooting

| Issue | Fix |
|-------|-----|
| Frontend can't reach API | Check `REACT_APP_API_URL` matches API URL; redeploy frontend |
| CORS error | Set `CORS_ORIGINS=https://your-frontend.onrender.com` on API |
| AI not responding | Verify `GITHUB_TOKEN` on API service |
| DB errors | Check Atlas IP whitelist and `MONGODB_URI` |
| Build timeout | Retry deploy; first PyTorch install takes ~10–15 min |
| `ResolutionImpossible` | Use latest `requirements-render.txt` (slim deps) + redeploy |

---

## 9. Update production

Push to GitHub `main` — Render auto-deploys if enabled on each service.

```bash
git push origin main
```
