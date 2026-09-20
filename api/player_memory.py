"""
Player-scoped memory, keyed on Discord identity.

`api/memory.py` handles SCENE memory: per-channel, ends with the scene. This
module handles PERSON memory, which outlives it. The distinction matters for
the world brief's core constraint:

    players come and go and affect the world,
    but the world must not stop waiting on them

Which gives three rules:

  1. Identity is the Discord id -- not the channel, not the session. A player
     can vanish for three months, return in a different channel under a new
     display name, and the world still knows who they are.
  2. A returning player is *briefed*, not rewound. Absence is reconciled from
     WorldEvents dated after their last_seen_at, so the world advances on its
     own clock while they are gone.
  3. Cost per request is bounded by importance-ranked selection, not by how
     long someone has played. A two-year veteran costs the same per message as
     a newcomer, because history is compacted into a dossier rather than
     replayed.

Compaction runs on the local backend (see settings.memory_compact_backend):
it is off the interactive path, so carrying deep history costs nothing at the
paid provider.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import httpx
from sqlalchemy.orm import Session

from .memory import estimate_tokens
from .models import Player, PlayerEvent, PlayerMemory, WorldEvent
from .settings import settings

__all__ = [
    "get_or_create_player_memory",
    "record_player_event",
    "record_world_event",
    "load_player_context",
    "touch_last_seen",
    "maybe_compact_player",
]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def get_or_create_player_memory(db: Session, player: Player) -> PlayerMemory:
    pm = db.query(PlayerMemory).filter(PlayerMemory.player_id == player.id).first()
    if pm is None:
        pm = PlayerMemory(player_id=player.id, summarised_through_event_id=0)
        db.add(pm)
        db.flush()
    return pm


def record_player_event(
    db: Session,
    player: Player,
    *,
    content: str,
    kind: str = "action",
    importance: int = 2,
    conversation_id: Optional[int] = None,
    world: str = "Overworld Nexus",
) -> PlayerEvent:
    """Log something this player did that the world should remember."""
    ev = PlayerEvent(
        player_id=player.id,
        world=world,
        kind=kind,
        content=content,
        importance=max(1, min(5, int(importance))),
        conversation_id=conversation_id,
    )
    db.add(ev)
    db.flush()
    return ev


def record_world_event(
    db: Session,
    *,
    headline: str,
    body: Optional[str] = None,
    faction: Optional[str] = None,
    importance: int = 2,
    created_by: str = "daemon",
    world: str = "Overworld Nexus",
) -> WorldEvent:
    """
    Log something that happened to the world itself.

    These are what absent players get caught up on. Headlines should read as
    one-line news the world would carry: "The Choir lost the Verge relay."
    """
    ev = WorldEvent(
        world=world,
        headline=headline,
        body=body,
        faction=faction,
        importance=max(1, min(5, int(importance))),
        created_by=created_by,
    )
    db.add(ev)
    db.flush()
    return ev


def touch_last_seen(db: Session, player: Player) -> None:
    """Mark the player present now. Drives the absence digest on their return."""
    pm = get_or_create_player_memory(db, player)
    pm.last_seen_at = datetime.utcnow()
    db.flush()


# ---------------------------------------------------------------------------
# Absence reconciliation
# ---------------------------------------------------------------------------

def _absence_digest(
    db: Session,
    pm: PlayerMemory,
    *,
    world: str,
    budget_tokens: int,
) -> Optional[str]:
    """
    What changed in the world while this player was away.

    None for players who were never away (or never here). This is the
    mechanism that lets the world move without them.
    """
    if pm.last_seen_at is None:
        return None

    gap = datetime.utcnow() - pm.last_seen_at
    if gap.total_seconds() < settings.memory_absence_seconds:
        return None

    rows = (
        db.query(WorldEvent)
        .filter(WorldEvent.world == world)
        .filter(WorldEvent.occurred_at > pm.last_seen_at)
        .order_by(WorldEvent.importance.desc(), WorldEvent.occurred_at.desc())
        .limit(settings.memory_max_world_events)
        .all()
    )
    if not rows:
        return None

    days = max(1, int(gap.total_seconds() // 86400))
    lines: List[str] = []
    remaining = budget_tokens

    for row in rows:
        line = f"- {row.headline}"
        cost = estimate_tokens(line)
        if cost > remaining:
            break
        lines.append(line)
        remaining -= cost

    if not lines:
        return None

    return (
        f"This player has been off the mesh for roughly {days} day(s). "
        "The world moved without them. Since they were last seen:\n"
        + "\n".join(lines)
        + "\n\nAcknowledge the gap in-world if it fits the moment. Do not "
        "replay events they were present for."
    )


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def load_player_context(
    db: Session,
    player: Player,
    *,
    budget_tokens: int,
    world: str = "Overworld Nexus",
) -> List[dict]:
    """
    Assemble this player's memory as system messages, within budget.

    Spend order: dossier (who they are), faction standing, absence digest
    (what they missed), then salient events by importance.
    """
    pm = get_or_create_player_memory(db, player)
    out: List[dict] = []
    remaining = budget_tokens

    # 1. Dossier -- compacted identity and history.
    if pm.dossier:
        cost = estimate_tokens(pm.dossier)
        if cost <= remaining:
            handle = player.primary_handle or player.display_name or player.discord_id
            out.append({
                "role": "system",
                "content": (
                    f"Dossier for the player you are speaking with ({handle}). "
                    f"Settled canon about who they are:\n\n{pm.dossier}"
                ),
            })
            remaining -= cost

    # 2. Faction standing -- cheap, and high value in a faction world.
    if isinstance(pm.standing, dict) and pm.standing:
        standing = ", ".join(f"{k}: {v:+d}" for k, v in sorted(pm.standing.items()))
        cost = estimate_tokens(standing)
        if cost <= remaining:
            out.append({
                "role": "system",
                "content": f"Faction standing for this player: {standing}",
            })
            remaining -= cost

    # 3. Absence digest -- the world kept moving.
    digest_budget = min(remaining, settings.memory_absence_tokens)
    if digest_budget > 0:
        digest = _absence_digest(db, pm, world=world, budget_tokens=digest_budget)
        if digest:
            out.append({"role": "system", "content": digest})
            remaining -= estimate_tokens(digest)

    # 4. Events not yet folded into the dossier, ranked by importance then
    #    recency -- a betrayal outranks yesterday's small talk.
    if remaining > 0:
        rows = (
            db.query(PlayerEvent)
            .filter(PlayerEvent.player_id == player.id)
            .filter(PlayerEvent.id > (pm.summarised_through_event_id or 0))
            .order_by(PlayerEvent.importance.desc(), PlayerEvent.id.desc())
            .limit(settings.memory_max_player_events)
            .all()
        )
        lines: List[str] = []
        for row in rows:
            line = f"- ({row.kind}) {row.content}"
            cost = estimate_tokens(line)
            if cost > remaining:
                break
            lines.append(line)
            remaining -= cost
        if lines:
            out.append({
                "role": "system",
                "content": "Recent consequential actions by this player:\n"
                           + "\n".join(lines),
            })

    return out


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

_PLAYER_COMPACT_INSTRUCTION = (
    "You maintain persistent dossiers on people moving through the Overworld "
    "Nexus. Fold the new events below into the existing dossier.\n\n"
    "Keep: identity, allegiances, capabilities, grudges, debts, standing "
    "commitments, and anything a later scene would contradict if forgotten.\n"
    "Drop: routine chatter, and anything superseded by a later event.\n\n"
    "Return the complete updated dossier as plain declarative prose, under "
    "200 words. No preamble, no headings, no commentary about the dossier."
)


async def maybe_compact_player(
    http: httpx.AsyncClient,
    db: Session,
    player: Player,
) -> bool:
    """
    Fold a player's accumulated events into their dossier.

    Same economics as scene compaction: local backend, off the interactive
    path, so deep history costs nothing at the paid provider. Failures are
    swallowed -- a missed compaction costs a few tokens next request and must
    never break a player's reply.
    """
    from .llm import LLMError, generate_with

    pm = get_or_create_player_memory(db, player)
    watermark = pm.summarised_through_event_id or 0

    pending = (
        db.query(PlayerEvent)
        .filter(PlayerEvent.player_id == player.id)
        .filter(PlayerEvent.id > watermark)
        .order_by(PlayerEvent.id.asc())
        .all()
    )
    if len(pending) < settings.memory_compact_after:
        return False

    events = "\n".join(
        f"- ({e.kind}, importance {e.importance}) {e.content}" for e in pending
    )
    prior = f"Existing dossier:\n{pm.dossier}\n\n" if pm.dossier else ""

    try:
        dossier = await generate_with(
            http,
            [
                {"role": "system", "content": _PLAYER_COMPACT_INSTRUCTION},
                {"role": "user", "content": f"{prior}New events:\n{events}"},
            ],
            backend=settings.memory_compact_backend,
            max_tokens=settings.memory_summary_tokens,
            temperature=0.2,
        )
    except (httpx.ReadTimeout, httpx.ConnectError, LLMError) as e:
        print(f"[memory] player compaction skipped: {e!r}", flush=True)
        return False

    dossier = (dossier or "").strip()
    if not dossier:
        return False

    pm.dossier = dossier
    pm.summarised_through_event_id = pending[-1].id
    db.flush()
    print(
        f"[memory] compacted {len(pending)} events for player {player.id} "
        f"-> {estimate_tokens(dossier)} tok dossier",
        flush=True,
    )
    return True
