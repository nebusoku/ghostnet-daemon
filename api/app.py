from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import httpx

from .schemas import ChatRequest, ChatResponse, IngestRequest, SearchRequest
from .deps import api_key_auth, clients
from .rag import upsert_texts, search_similar
from .settings import settings

app = FastAPI(title="GhostNet Daemon — AI Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
async def health(_: None = Depends(api_key_auth)): 
    return {"status": "ok"}

# Keep your small-box defaults
OLLAMA_OPTS = {"num_ctx":512,"num_predict":64,"temperature":0.6,"repeat_penalty":1.1,"num_thread":6}

async def ollama_chat(http: httpx.AsyncClient, messages: List[dict]) -> str:
    r = await http.post(
        f"{settings.ollama_url}/api/chat",
        json={"model":settings.chat_model,"messages":messages,"stream":False,"options":OLLAMA_OPTS},
        timeout=120
    )
    r.raise_for_status()
    d = r.json()
    if isinstance(d, dict):
        if "message" in d and "content" in d["message"]: return d["message"]["content"]
        if "response" in d: return d["response"]
    raise RuntimeError(f"Unexpected chat response: {d}")

def trim(s: str, n: int) -> str: 
    return s if len(s) <= n else s[:n] + "…"

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _: None = Depends(api_key_auth)):
    msgs = []

    # Base policy to reduce hallucinations
    base_policy = (
        "Policy: You are GhostNet Daemon for the Eris-Lily-Cyber project. "
        "Prefer concise, correct answers. If you lack relevant context, say you don’t know. "
        "Do not guess or invent facts. Ignore unrelated topics (e.g., any malware named GhostNet)."
    )
    msgs.append({"role":"system","content": base_policy})

    if req.system:
        msgs.append({"role":"system","content": req.system})

    msgs.extend([m.model_dump() for m in req.messages])

    # RAG with guardrails
    if req.rag:
        user = next((m.content for m in req.messages[::-1] if m.role=="user"), "")
        try:
            hits = await search_similar(clients.http, clients.qdrant, user, settings.top_k)
        except Exception:
            hits = []

        # Only keep strong matches; cosine score closer to 1.0 is better
        strong = [d for d, s in hits if s >= 0.75][:3]
        if strong:
            ctx = trim("\n\n".join(strong), 800)  # small boxes stay snappy
            msgs.insert(0, {"role":"system","content":
                "You must answer using ONLY the context below. "
                "If it is insufficient or unrelated, say you don’t know.\n\n" + ctx
            })
        else:
            msgs.insert(0, {"role":"system","content":
                "If you lack relevant context for the user’s question, say you don’t know. Do not guess."
            })

    try:
        content = await ollama_chat(clients.http, msgs)
    except (httpx.ReadTimeout, httpx.ConnectError) as e:
        content = f"(timeout talking to local model: {e})"
    return ChatResponse(content=content)

@app.post("/ingest")
async def ingest(req: IngestRequest, _: None = Depends(api_key_auth)):
    await upsert_texts(clients.http, clients.qdrant, req.texts, req.metadatas or [])
    return {"added": len(req.texts)}

@app.post("/search")
async def search(req: SearchRequest, _: None = Depends(api_key_auth)):
    k = req.top_k or settings.top_k
    hits = await search_similar(clients.http, clients.qdrant, req.query, k)
    return {"results":[{"text":t,"score":s} for t,s in hits]}
