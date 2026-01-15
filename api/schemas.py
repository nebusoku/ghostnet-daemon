from typing import List, Optional
from pydantic import BaseModel, ConfigDict


# ---------------------------
# Chat / RAG schemas
# ---------------------------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    system: Optional[str] = None
    rag: bool = True


class ChatResponse(BaseModel):
    content: str


class IngestRequest(BaseModel):
    texts: List[str]
    metadatas: Optional[List[dict]] = None


class SearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = None


# ---------------------------
# World document schemas
# ---------------------------

class WorldDocIn(BaseModel):
    world: str = "Overworld Nexus"
    kind: str  # "lore", "npc", "location", etc.
    title: Optional[str] = None
    body: str
    tags: Optional[List[str]] = None
    status: str = "active"
    created_by: Optional[str] = "user"
    created_from_message_id: Optional[int] = None

class LogMessageIn(BaseModel):
    """
    Payload for logging a single chat message from Discord (or other sources).
    """
    source: str = "discord"           # e.g. "discord"
    channel_id: str                   # Discord channel or thread ID
    message_id: str                   # Discord message ID

    user_id: Optional[str] = None     # Discord user ID if applicable
    role: str = "user"                # "user" / "assistant" / "system" / etc.
    content: str

    meta: Optional[dict] = None       # extra metadata (guild, names, etc.)


# ---------------------------
# Player schemas
# ---------------------------

class PlayerBase(BaseModel):
    discord_id: str
    primary_handle: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_npc: bool = False
    aliases: List[str] = []
    mature_ok: Optional[bool] = False


class PlayerCreate(PlayerBase):
    """Data needed to create or upsert a Player."""
    pass


class PlayerUpdate(BaseModel):
    """Partial update for Player."""
    primary_handle: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_npc: Optional[bool] = None
    aliases: Optional[List[str]] = None
    mature_ok: Optional[bool] = None


class PlayerOut(PlayerBase):
    """What we return from the API."""
    id: int
    model_config = ConfigDict(from_attributes=True)
