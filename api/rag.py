from typing import List, Tuple
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct
from .settings import settings
import httpx

async def embed_texts(http: httpx.AsyncClient, texts: List[str]) -> List[List[float]]:
    # Ollama may return {"embeddings":[...]} or {"embedding":[...]} (single)
    r = await http.post(
        f"{settings.ollama_url}/api/embeddings",
        json={"model": settings.embed_model, "input": texts},
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    if "embeddings" in d:
        return d["embeddings"]
    if "embedding" in d:
        return [d["embedding"]]
    raise RuntimeError(f"Unexpected embeddings response: {d}")

async def upsert_texts(http: httpx.AsyncClient, qc: QdrantClient, texts: List[str], metas: List[dict] | None=None):
    vecs = await embed_texts(http, texts)
    dim = len(vecs[0])

    # Create collection if missing
    try:
        qc.get_collection(settings.collection)
    except Exception:
        qc.recreate_collection(
            settings.collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )

    points: List[PointStruct] = []
    for i, (v, t) in enumerate(zip(vecs, texts)):
        meta = {} if not metas or i >= len(metas) or metas[i] is None else metas[i]
        meta = {**meta, "text": t}
        # Do NOT pass id=None; omit id to auto-generate
        points.append(PointStruct(vector=v, payload=meta))

    qc.upsert(collection_name=settings.collection, points=points)

async def search_similar(http: httpx.AsyncClient, qc: QdrantClient, query: str, top_k: int) -> List[Tuple[str, float]]:
    qv = (await embed_texts(http, [query]))[0]
    res = qc.search(
        collection_name=settings.collection,
        query_vector=qv,
        limit=top_k,
        with_payload=True,
    )
    return [(p.payload.get("text", ""), float(p.score)) for p in res]
