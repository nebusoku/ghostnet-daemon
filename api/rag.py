from typing import List, Tuple
import uuid

import httpx
from qdrant_client import QdrantClient, models

from .settings import settings
from .models import WorldDocument


# --------- Embeddings --------- #

async def embed_texts(http: httpx.AsyncClient, texts: List[str]) -> List[List[float]]:
    """
    Call Ollama embeddings (Ollama 0.12.x style).

    - Uses /api/embeddings
    - Body: {"model": "<name>", "prompt": "<single string>"}
    - Returns: {"embedding": [float, ...], ...}

    We call Ollama once per text and collect the vectors.
    """
    embeddings: List[List[float]] = []

    for t in texts:
        r = await http.post(
            f"{settings.ollama_url}/api/embeddings",
            json={"model": settings.embed_model, "prompt": t},
            timeout=120,
        )
        r.raise_for_status()
        d = r.json()

        # Ollama currently returns {"embedding": [...], "num_tokens": ...}
        vec = d.get("embedding")
        if not isinstance(vec, list) or len(vec) == 0:
            raise RuntimeError(f"Unexpected embedding response for text={t!r}: {d}")

        embeddings.append(vec)

    return embeddings


def _ensure_collection(qc: QdrantClient, dim: int) -> None:
    """
    Make sure the main collection exists.

    IMPORTANT:
    - We do NOT try to create it here anymore, because of a
      VectorsConfig JSON mismatch between client/server.
    - You must create the collection manually once, e.g.:

        from qdrant_client import QdrantClient, models
        qc = QdrantClient(url="http://localhost:6333")
        qc.recreate_collection(
            collection_name="ghostnet_docs",
            vectors_config=models.VectorParams(
                size=768,
                distance=models.Distance.COSINE,
            ),
        )

    After that, this function only checks that it exists.
    """
    name = settings.collection

    if not qc.collection_exists(collection_name=name):
        raise RuntimeError(
            f"Qdrant collection '{name}' does not exist. "
            "Create it manually once (see _ensure_collection docstring)."
        )
    # We intentionally do NOT recreate/modify the collection here.


# --------- Generic RAG used by /ingest + /search --------- #


async def upsert_texts(
    http: httpx.AsyncClient,
    qc: QdrantClient,
    texts: List[str],
    metas: List[dict] | None = None,
):
    """
    Original generic RAG path, used by /ingest and /search.
    Stores raw texts with optional metadata.
    """
    vecs = await embed_texts(http, texts)
    if not vecs:
        return

    dim = len(vecs[0])
    _ensure_collection(qc, dim)

    points: List[models.PointStruct] = []
    for i, (v, t) in enumerate(zip(vecs, texts)):
        meta = {} if not metas or i >= len(metas) or metas[i] is None else metas[i]
        meta = {**meta, "text": t}
        points.append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=v,
                payload=meta,
            )
        )

    qc.upsert(collection_name=settings.collection, points=points)


async def search_similar(
    http: httpx.AsyncClient,
    qc: QdrantClient,
    query: str,
    top_k: int,
) -> List[Tuple[str, float]]:
    """
    Search in the generic RAG collection by similarity to the query string.
    Returns (text, score) pairs.
    """
    qv = (await embed_texts(http, [query]))[0]
    res = qc.search(
        collection_name=settings.collection,
        query_vector=qv,
        limit=top_k,
        with_payload=True,
    )
    return [(p.payload.get("text", ""), float(p.score)) for p in res]


# --------- World-document specific RAG --------- #


async def upsert_world_documents(
    http: httpx.AsyncClient,
    qc: QdrantClient,
    db,
    docs: List[WorldDocument],
):
    """
    Store world docs in the same Qdrant collection used for general RAG.

    Each point payload keeps world/kind/title/body/tags/status/etc,
    plus doc_id so we can cross-link back to SQL later.
    """
    if not docs:
        return

    texts = [doc.body for doc in docs]
    vecs = await embed_texts(http, texts)
    if not vecs:
        return

    dim = len(vecs[0])
    _ensure_collection(qc, dim)

    points: List[models.PointStruct] = []

    for doc, vec in zip(docs, vecs):
        payload = {
            "world": doc.world,
            "kind": doc.kind,
            "title": doc.title,
            "body": doc.body,
            "tags": doc.tags or [],
            "status": doc.status,
            "created_by": doc.created_by,
            "doc_id": doc.id,
        }

        points.append(
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload=payload,
            )
        )

    qc.upsert(collection_name=settings.collection, points=points)
    db.commit()
