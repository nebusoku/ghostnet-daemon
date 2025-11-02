from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import httpx
from schemas import ChatRequest, ChatResponse, IngestRequest, SearchRequest
from deps import api_key_auth, clients
from rag import upsert_texts, search_similar
from settings import settings
app = FastAPI(title="GhostNet Daemon — AI Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
@app.get("/health")
async def health(_: None = Depends(api_key_auth)): return {"status": "ok"}
OLLAMA_OPTS = {"num_ctx":512,"num_predict":64,"temperature":0.6,"repeat_penalty":1.1,"num_thread":6}
async def ollama_chat(http: httpx.AsyncClient, messages: List[dict]) -> str:
    r = await http.post(f"{settings.ollama_url}/api/chat", json={"model":settings.chat_model,"messages":messages,"stream":False,"options":OLLAMA_OPTS}, timeout=120)
    r.raise_for_status()
    d=r.json()
    if isinstance(d,dict):
        if "message" in d and "content" in d["message"]: return d["message"]["content"]
        if "response" in d: return d["response"]
    raise RuntimeError(f"Unexpected chat response: {d}")
def trim(s: str, n: int) -> str: return s if len(s)<=n else s[:n]+"…"
@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _: None = Depends(api_key_auth)):
    msgs=[]
    if req.system: msgs.append({"role":"system","content":req.system})
    msgs.extend([m.model_dump() for m in req.messages])
    if req.rag:
        user = next((m.content for m in req.messages[::-1] if m.role=="user"), "")
        try: hits = await search_similar(clients.http, clients.qdrant, user, 2)
        except Exception: hits=[]
        ctx = trim("\n\n".join([d for d,_ in hits]), 600)
        if ctx: msgs.insert(0, {"role":"system","content":f"Use the following context if relevant.\n\n{ctx}"})
    try: content = await ollama_chat(clients.http, msgs)
    except (httpx.ReadTimeout, httpx.ConnectError) as e: content = f"(timeout talking to local model: {e})"
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
