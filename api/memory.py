"""
Token-budgeted conversation memory.

The naive approach -- append every turn and resend the whole transcript -- grows
linearly forever. A busy Discord channel would be paying to resend the same
opening scene hundreds of times. This module exists so prompt cost stays flat
regardless of how long a conversation runs.

Four things keep the bill down:

  1. Sliding window. Only recent turns are sent verbatim.
  2. Rolling summary. Older turns are compacted once into a summary that is
     carried forward, instead of being resent in full every request.
  3. Compaction runs on the LOCAL backend. Summarising is a slow-path job with
     no user waiting on it, so it runs on the VM's own model at zero marginal
     cost -- the paid backend is only ever used for live play.
  4. Hard budget. The context is assembled against an explicit token budget and
     truncated to fit BEFORE the call, rather than hoping it fits.

Deflected probes (see api/guard.py) are recorded for the audit trail but are
excluded from the context window -- "2+2" and its brush-off should not occupy
budget that belongs to the scene.

Schema note: the rolling summary lives in Conversation.world_state (JSON,
previously unused) rather than a new column, because this project has no
migration tool -- SQLAlchemy's create_all() adds tables but never alters
existing ones. Revisit if Alembic lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import httpx
from sqlalchemy.orm import Session

from .models import Conversation, Message
from .db import utcnow
from .settings import settings

__all__ = [
    "ContextBundle",
    "estimate_tokens",
    "get_or_create_conversation",
    "record_message",
    "load_context",
    "maybe_compact",
]


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------
# Deliberately dependency-free. tiktoken would be exact for OpenAI models but
# wrong for Llama/Mistral tokenisers and adds a build dependency for a number
# we only use to decide what to drop. Chars-per-token is the standard budgeting
# heuristic; TOKEN_CHARS_PER is tunable, and callers leave headroom.

def estimate_tokens(text: str) -> int:
    """Approximate token count. Intentionally rough, biased to overestimate."""
    if not text:
        return 0
    return max(1, (len(text) + settings.token_chars_per - 1) // settings.token_chars_per)


def _messages_tokens(msgs: List[dict]) -> int:
    # ~4 tokens per message of role/formatting overhead.
    return sum(estimate_tokens(m.get("content", "")) + 4 for m in msgs)


# ---------------------------------------------------------------------------
# Context bundle
# ---------------------------------------------------------------------------

@dataclass
class ContextBundle:
    """Everything memory contributes to one request, already within budget."""
    conversation: Optional[Conversation] = None
    summary: Optional[str] = None
    recent: List[dict] = field(default_factory=list)
    # Observability: what the budget actually went on.
    summary_tokens: int = 0
    recent_tokens: int = 0
    dropped_turns: int = 0

    def as_messages(self) -> List[dict]:
        """Render as chat messages, summary first."""
        out: List[dict] = []
        if self.summary:
            out.append({
                "role": "system",
                "content": (
                    "Established record of this thread so far. Treat it as "
                    "settled canon and continue from it; do not re-narrate it.\n\n"
                    + self.summary
                ),
            })
        out.extend(self.recent)
        return out


# ---------------------------------------------------------------------------
# Conversation + message persistence
# ---------------------------------------------------------------------------

def get_or_create_conversation(
    db: Session,
    *,
    external_id: str,
    source: str = "discord",
    title: Optional[str] = None,
) -> Conversation:
    """Fetch the Conversation for a channel/thread, creating it if needed."""
    convo = (
        db.query(Conversation)
        .filter(
            Conversation.external_id == str(external_id),
            Conversation.source == source,
        )
        .first()
    )
    if convo is None:
        convo = Conversation(
            external_id=str(external_id),
            source=source,
            title=title,
            world_state={},
        )
        db.add(convo)
        db.flush()
    return convo


def record_message(
    db: Session,
    convo: Conversation,
    *,
    role: str,
    content: str,
    model: Optional[str] = None,
    meta: Optional[dict] = None,
) -> Message:
    """Append one message to a conversation."""
    msg = Message(
        conversation_id=convo.id,
        role=role,
        content=content,
        model=model,
        meta=meta,
    )
    db.add(msg)
    convo.updated_at = utcnow()
    db.flush()
    return msg


def _summary_state(convo: Conversation) -> dict:
    state = convo.world_state if isinstance(convo.world_state, dict) else {}
    return state


def get_summary(convo: Conversation) -> Optional[str]:
    return _summary_state(convo).get("summary") or None


def _set_summary(convo: Conversation, summary: str, through_id: int) -> None:
    state = dict(_summary_state(convo))
    state["summary"] = summary
    state["summary_through_message_id"] = through_id
    # Reassign (not mutate) so SQLAlchemy marks the JSON column dirty.
    convo.world_state = state


def _summarised_through(convo: Conversation) -> int:
    return int(_summary_state(convo).get("summary_through_message_id") or 0)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def load_context(
    db: Session,
    convo: Conversation,
    *,
    budget_tokens: int,
    exclude_message_id: Optional[int] = None,
) -> ContextBundle:
    """
    Assemble the in-budget conversation context.

    Pulls turns newer than the summary watermark, newest-first, keeping them
    until the budget is spent, then restores chronological order. Deflections
    are skipped -- they are logged history, not scene content.
    """
    bundle = ContextBundle(conversation=convo)

    summary = get_summary(convo)
    remaining = budget_tokens

    if summary:
        bundle.summary = summary
        bundle.summary_tokens = estimate_tokens(summary) + 4
        remaining -= bundle.summary_tokens

    watermark = _summarised_through(convo)

    q = (
        db.query(Message)
        .filter(Message.conversation_id == convo.id)
        .filter(Message.id > watermark)
    )
    if exclude_message_id is not None:
        q = q.filter(Message.id != exclude_message_id)

    # Newest first so the budget is spent on the most relevant turns.
    rows = q.order_by(Message.id.desc()).limit(settings.memory_max_turns).all()

    kept: List[dict] = []
    for row in rows:
        if (row.meta or {}).get("deflected"):
            continue
        if row.role not in ("user", "assistant"):
            continue

        cost = estimate_tokens(row.content) + 4
        if cost > remaining:
            bundle.dropped_turns += 1
            continue

        kept.append({"role": row.role, "content": row.content})
        remaining -= cost

    kept.reverse()  # back to chronological
    bundle.recent = kept
    bundle.recent_tokens = _messages_tokens(kept)
    return bundle


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

_COMPACT_INSTRUCTION = (
    "You are maintaining the running record of an ongoing scene in the "
    "Overworld Nexus. Condense the exchange below into a compact continuity "
    "note for whoever picks the scene up next.\n\n"
    "Keep: who is present, what they did and decided, what changed in the "
    "world, unresolved threads, and any established fact a later scene would "
    "contradict if forgotten.\n"
    "Drop: atmosphere, description, and anything already implied.\n\n"
    "Write plain declarative prose in past tense. No preamble, no headings, "
    "no commentary about the summary itself."
)


async def maybe_compact(
    http: httpx.AsyncClient,
    db: Session,
    convo: Conversation,
) -> bool:
    """
    Fold older turns into the rolling summary when a conversation has grown
    past the compaction threshold.

    Returns True if a compaction ran. Summarisation is a slow-path job with no
    user waiting, so it always uses the local backend regardless of
    LLM_BACKEND -- that is the point, not a limitation.

    Failures are swallowed: a missed compaction costs some tokens on the next
    request, but must never break a player's reply.
    """
    # Imported here: api.llm imports nothing from this module, but keeping the
    # import local documents that compaction is the only coupling.
    from .llm import LLMError, generate_with

    watermark = _summarised_through(convo)

    pending = (
        db.query(Message)
        .filter(Message.conversation_id == convo.id)
        .filter(Message.id > watermark)
        .order_by(Message.id.asc())
        .all()
    )
    pending = [m for m in pending if not (m.meta or {}).get("deflected")]

    if len(pending) < settings.memory_compact_after:
        return False

    # Leave the most recent turns uncompacted so the live window still has
    # verbatim detail to work with.
    keep_live = settings.memory_keep_verbatim
    to_compact = pending[:-keep_live] if keep_live else pending
    if not to_compact:
        return False

    transcript = "\n".join(
        f"{m.role}: {m.content}" for m in to_compact
    )

    existing = get_summary(convo)
    prior = f"Record so far:\n{existing}\n\n" if existing else ""

    messages = [
        {"role": "system", "content": _COMPACT_INSTRUCTION},
        {"role": "user", "content": f"{prior}New exchange:\n{transcript}"},
    ]

    try:
        summary = await generate_with(
            http,
            messages,
            backend=settings.memory_compact_backend,
            max_tokens=settings.memory_summary_tokens,
            temperature=0.2,
        )
    except (httpx.ReadTimeout, httpx.ConnectError, LLMError) as e:
        print(f"[memory] compaction skipped: {e!r}", flush=True)
        return False

    summary = (summary or "").strip()
    if not summary:
        return False

    _set_summary(convo, summary, to_compact[-1].id)
    db.flush()
    print(
        f"[memory] compacted {len(to_compact)} turns for convo {convo.id} "
        f"-> {estimate_tokens(summary)} tok summary",
        flush=True,
    )
    return True
