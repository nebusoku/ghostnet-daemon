from datetime import datetime
from typing import Optional, Any

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import relationship
from .db import Base

class Conversation(Base):
    """
    A chat session, e.g. one Discord channel / thread / DM.
    """
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String, index=True, nullable=True)   # e.g. Discord channel/thread id
    source = Column(String, default="discord", index=True)    # discord, cli, etc.
    title = Column(String, nullable=True)

    world_state = Column(JSON, nullable=True)                 # avatar/faction/etc
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class Message(Base):
    """
    Individual chat messages within a Conversation.
    """
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)

    role = Column(String, nullable=False)        # user / assistant / system / tool
    content = Column(Text, nullable=False)
    model = Column(String, nullable=True)
    meta = Column("metadata", JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class WorldDocument(Base):
    """
    Canonical world knowledge: lore, NPC bios, locations, factions, rules, etc.
    This is what we index into Qdrant and feed back into Ollama.
    """
    __tablename__ = "world_documents"

    id = Column(Integer, primary_key=True, index=True)
    world = Column(String, default="Overworld Nexus", index=True)

    kind = Column(String, index=True)            # lore / npc / location / faction / rule / etc.
    title = Column(String, nullable=True)
    body = Column(Text, nullable=False)

    tags = Column(JSON, nullable=True)           # ["lily", "eris", "corp-HQ"]
    status = Column(String, default="active")    # active / deprecated / retconned

    created_from_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
    created_by = Column(String, nullable=True)   # user / daemon / import-json

    qdrant_point_id = Column(String, nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
)



class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, index=True)

    # Discord identity
    discord_id = Column(String, unique=True, index=True, nullable=False)

    # Primary naming + display
    primary_handle = Column(String, nullable=False)   # e.g. "Nebusoku"
    display_name = Column(String, nullable=True)      # current server display name

    # Optional avatar URL / CDN link
    avatar_url = Column(String, nullable=True)

    # NPC / human flag
    is_npc = Column(Boolean, nullable=False, default=False)

    # Adult-content preference (opt-in)
    mature_ok = Column(Boolean, nullable=False, default=False)

    # JSON-encoded list of alias strings (Tupperbox names, RP aliases, etc.)
    aliases = Column(Text, nullable=False, default="[]")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

class Entity(Base):
    """
    Structured world entities – characters, locations, orgs, etc.
    """
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, index=True)
    world = Column(String, default="Overworld Nexus", index=True)

    name = Column(String, unique=True, index=True)
    type = Column(String, index=True)           # character / location / org / item / etc.
    summary = Column(Text, nullable=True)
    data = Column(JSON, nullable=True)          # arbitrary structured info (stats, tags, etc.)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class PlayerMemory(Base):
    """
    Per-player rolling dossier, keyed to a Discord identity.

    Scene memory (Conversation.world_state) is per-channel and dies with the
    scene. This is per-PERSON and outlives it: a player can vanish for three
    months, come back in a different channel, and the world still knows who
    they are and what they did.

    One row per player. The dossier is compacted, not appended, so a player
    with two years of history costs the same per request as a new one.
    """
    __tablename__ = "player_memory"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), unique=True, nullable=False, index=True)

    dossier = Column(Text, nullable=True)          # compacted "who this is"
    standing = Column(JSON, nullable=True)         # {"spire": -2, "choir": 1}

    last_seen_at = Column(DateTime, nullable=True, index=True)
    # Watermark: events at/below this id are already folded into `dossier`.
    summarised_through_event_id = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PlayerEvent(Base):
    """
    Append-only log of things a player did that the world should remember.

    Scored by importance so context assembly can select the most consequential
    events rather than merely the most recent -- a faction betrayal six months
    ago outranks yesterday's small talk, and selecting by importance keeps the
    per-request cost bounded no matter how long someone has played.
    """
    __tablename__ = "player_events"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)

    world = Column(String, default="Overworld Nexus", index=True)
    kind = Column(String, index=True)        # action / decision / relationship / arc
    content = Column(Text, nullable=False)

    # 1 = incidental, 5 = world-altering. Drives selection under a token budget.
    importance = Column(Integer, default=2, nullable=False, index=True)

    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class WorldEvent(Base):
    """
    Things that happened to the world itself, independent of any player.

    This is what lets the world keep moving while someone is away: on return,
    a player is caught up from WorldEvents dated after their last_seen_at,
    rather than the scene having been frozen waiting for them.
    """
    __tablename__ = "world_events"

    id = Column(Integer, primary_key=True, index=True)
    world = Column(String, default="Overworld Nexus", index=True)

    headline = Column(String, nullable=False)    # one-line, for catch-up digests
    body = Column(Text, nullable=True)
    faction = Column(String, nullable=True, index=True)
    importance = Column(Integer, default=2, nullable=False, index=True)

    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_by = Column(String, nullable=True)   # daemon / gm / system
