from pydantic import BaseModel
from typing import List, Optional
class ChatMessage(BaseModel): role: str; content: str
class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    system: Optional[str] = None
    rag: bool = True
class ChatResponse(BaseModel): content: str
class IngestRequest(BaseModel):
    texts: List[str]
    metadatas: Optional[List[dict]] = None
class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None
