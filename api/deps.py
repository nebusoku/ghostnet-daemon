from fastapi import Header, HTTPException
import httpx
from qdrant_client import QdrantClient
from .settings import settings
async def api_key_auth(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    if token != settings.api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
class Clients:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=60)
        self.qdrant = QdrantClient(url=settings.qdrant_url)
clients = Clients()
