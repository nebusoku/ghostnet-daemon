#!/usr/bin/env python3
"""
Inspect and clean the GhostNet memory stores in Postgres.

Run from the repo root on the API host, with the same DATABASE_URL the API uses:

    python scripts/memory_tool.py stats
    python scripts/memory_tool.py conversation 1438390800676552786
    python scripts/memory_tool.py player 268551710417354752

EVERY destructive command is a DRY RUN by default and prints exactly what it
would touch. Nothing is deleted until you add --apply. This is deliberate:
the RAG corpus on this project has already been poisoned once by an
unreviewed bulk ingest, and memory is harder to reconstruct than it looks.

    python scripts/memory_tool.py purge-deflected                # preview
    python scripts/memory_tool.py purge-deflected --apply        # do it

Commands
--------
  stats                  footprint of every memory store, with token estimates
  conversations          largest scenes by stored turns
  conversation <id>      one scene: summary, watermark, recent turns
  player <discord_id>    one player: dossier, standing, events, last seen
  events                 recent player events, optionally filtered

  purge-deflected        drop guard-deflected turns (audit noise)
  prune-events           drop old low-importance player events
  forget-conversation    erase one scene's turns and summary
  forget-player          erase one person's dossier and events
  reset-summary          clear a scene summary so it recompacts from source
  purge-bot-docs         drop world_documents authored by a bot (RAG poison)
  vacuum                 delete orphaned rows
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

# Allow running as `python scripts/memory_tool.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, or_  # noqa: E402

from api.db import SessionLocal  # noqa: E402
from api.models import (  # noqa: E402
    Conversation,
    Message,
    Player,
    PlayerEvent,
    PlayerMemory,
    WorldDocument,
    WorldEvent,
)
from api.settings import settings  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def est_tokens(text) -> int:
    if not text:
        return 0
    return max(1, (len(text) + settings.token_chars_per - 1) // settings.token_chars_per)


def human(n: int) -> str:
    return f"{n:,}"


def rule(title: str = "") -> None:
    print(f"\n{'=' * 68}")
    if title:
        print(title)
        print("=" * 68)


def confirm(args, what: str, count: int) -> bool:
    """Dry-run gate. Returns True only when --apply was passed."""
    if count == 0:
        print(f"  nothing to do: no rows matched ({what})")
        return False
    if not args.apply:
        print(f"  DRY RUN: would delete {human(count)} row(s) -- {what}")
        print("  re-run with --apply to actually delete")
        return False
    print(f"  deleting {human(count)} row(s) -- {what}")
    return True


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------

def cmd_stats(db, args) -> None:
    rule("MEMORY FOOTPRINT")

    convos = db.query(func.count(Conversation.id)).scalar() or 0
    msgs = db.query(func.count(Message.id)).scalar() or 0
    players = db.query(func.count(Player.id)).scalar() or 0
    pmem = db.query(func.count(PlayerMemory.id)).scalar() or 0
    pevents = db.query(func.count(PlayerEvent.id)).scalar() or 0
    wevents = db.query(func.count(WorldEvent.id)).scalar() or 0
    wdocs = db.query(func.count(WorldDocument.id)).scalar() or 0

    print(f"  conversations    {human(convos)}")
    print(f"  messages         {human(msgs)}")
    print(f"  players          {human(players)}  (with memory rows: {human(pmem)})")
    print(f"  player events    {human(pevents)}")
    print(f"  world events     {human(wevents)}")
    print(f"  world documents  {human(wdocs)}")

    # Deflected turns are audit-only and excluded from context. If they are a
    # large share of stored messages, the bot is fielding a lot of probes.
    deflected = 0
    for (meta,) in db.query(Message.meta).filter(Message.meta.isnot(None)):
        if isinstance(meta, dict) and meta.get("deflected"):
            deflected += 1
    if msgs:
        print(f"  deflected turns  {human(deflected)} "
              f"({100 * deflected / msgs:.1f}% of messages)")

    rule("TOKEN ESTIMATES (stored, not per-request)")
    msg_tok = sum(est_tokens(c) for (c,) in db.query(Message.content))
    sum_tok = 0
    for (ws,) in db.query(Conversation.world_state):
        if isinstance(ws, dict):
            sum_tok += est_tokens(ws.get("summary"))
    dos_tok = sum(est_tokens(d) for (d,) in db.query(PlayerMemory.dossier))
    print(f"  raw messages     {human(msg_tok)} tok")
    print(f"  scene summaries  {human(sum_tok)} tok")
    print(f"  player dossiers  {human(dos_tok)} tok")
    print(f"\n  per-request ceiling (config): "
          f"{settings.memory_scene_tokens + settings.memory_player_tokens} tok "
          f"(scene {settings.memory_scene_tokens} + player "
          f"{settings.memory_player_tokens})")
    print("  Stored volume does not affect per-request cost -- that is the point.")

    # RAG poisoning check: this project has been bitten before.
    rule("RAG CORPUS HEALTH")
    bot_docs = (
        db.query(func.count(WorldDocument.id))
        .filter(WorldDocument.body.like("[bot]%"))
        .scalar()
        or 0
    )
    leak_docs = (
        db.query(func.count(WorldDocument.id))
        .filter(
            or_(
                WorldDocument.body.ilike("%language model%"),
                WorldDocument.body.ilike("%as an AI%"),
                WorldDocument.body.ilike("%I cannot assist%"),
            )
        )
        .scalar()
        or 0
    )
    print(f"  world_documents authored by a bot : {human(bot_docs)}")
    print(f"  documents containing assistant-voice text: {human(leak_docs)}")
    if bot_docs or leak_docs:
        print("  ^ these are retrievable as 'canon'. See: purge-bot-docs")


def cmd_conversations(db, args) -> None:
    rule(f"LARGEST SCENES (top {args.limit})")
    rows = (
        db.query(
            Conversation.id,
            Conversation.external_id,
            Conversation.source,
            func.count(Message.id).label("n"),
        )
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .group_by(Conversation.id, Conversation.external_id, Conversation.source)
        .order_by(func.count(Message.id).desc())
        .limit(args.limit)
        .all()
    )
    if not rows:
        print("  (no conversations recorded yet)")
        return
    print(f"  {'id':>5}  {'turns':>6}  {'source':<9} external_id")
    for r in rows:
        print(f"  {r.id:>5}  {r.n:>6}  {r.source or '?':<9} {r.external_id}")


def _find_conversation(db, ident: str):
    convo = (
        db.query(Conversation)
        .filter(Conversation.external_id == str(ident))
        .first()
    )
    if convo is None and str(ident).isdigit():
        convo = db.get(Conversation, int(ident))
    return convo


def cmd_conversation(db, args) -> None:
    convo = _find_conversation(db, args.ident)
    if convo is None:
        print(f"no conversation matching {args.ident!r}")
        return

    state = convo.world_state if isinstance(convo.world_state, dict) else {}
    summary = state.get("summary")
    watermark = state.get("summary_through_message_id") or 0
    total = (
        db.query(func.count(Message.id))
        .filter(Message.conversation_id == convo.id)
        .scalar()
        or 0
    )

    rule(f"SCENE {convo.id}  ({convo.source}:{convo.external_id})")
    print(f"  stored turns      {human(total)}")
    print(f"  compacted through message id {watermark}")
    print(f"  summary           {est_tokens(summary)} tok"
          if summary else "  summary           (none yet)")
    if summary:
        print(f"\n  --- summary ---\n  {summary}\n")

    rows = (
        db.query(Message)
        .filter(Message.conversation_id == convo.id)
        .order_by(Message.id.desc())
        .limit(args.limit)
        .all()
    )
    print(f"  --- last {len(rows)} turns (newest first) ---")
    for m in reversed(rows):
        flag = ""
        if isinstance(m.meta, dict) and m.meta.get("deflected"):
            flag = f" [deflected:{m.meta.get('category', '?')}]"
        live = "" if m.id > watermark else " (compacted)"
        body = (m.content or "").replace("\n", " ")[:110]
        print(f"  #{m.id:<6} {m.role:<9}{flag}{live}  {body}")


def cmd_player(db, args) -> None:
    player = (
        db.query(Player).filter(Player.discord_id == str(args.discord_id)).first()
    )
    if player is None:
        print(f"no player with discord_id {args.discord_id}")
        return

    pm = db.query(PlayerMemory).filter(PlayerMemory.player_id == player.id).first()

    rule(f"PLAYER {player.primary_handle or player.discord_id}")
    print(f"  discord_id    {player.discord_id}")
    print(f"  display_name  {player.display_name}")
    print(f"  is_npc        {player.is_npc}   mature_ok  {player.mature_ok}")

    if pm is None:
        print("  (no memory row yet -- they have not spoken since memory was enabled)")
    else:
        seen = pm.last_seen_at
        if seen:
            gap = datetime.utcnow() - seen
            days = gap.total_seconds() / 86400
            away = (f"  ({days:.1f} days ago -- absence digest WOULD fire)"
                    if gap.total_seconds() >= settings.memory_absence_seconds
                    else f"  ({days:.1f} days ago)")
            print(f"  last seen     {seen}{away}")
        else:
            print("  last seen     (never)")
        print(f"  standing      {pm.standing or '(none)'}")
        print(f"  dossier       {est_tokens(pm.dossier)} tok, "
              f"compacted through event {pm.summarised_through_event_id}")
        if pm.dossier:
            print(f"\n  --- dossier ---\n  {pm.dossier}\n")

    rows = (
        db.query(PlayerEvent)
        .filter(PlayerEvent.player_id == player.id)
        .order_by(PlayerEvent.importance.desc(), PlayerEvent.id.desc())
        .limit(args.limit)
        .all()
    )
    print(f"  --- top {len(rows)} events by importance ---")
    for e in rows:
        mark = "" if pm is None or e.id > (pm.summarised_through_event_id or 0) \
            else " (folded)"
        print(f"  #{e.id:<6} imp{e.importance} {e.kind:<12}{mark}  "
              f"{(e.content or '')[:90]}")


def cmd_events(db, args) -> None:
    q = db.query(PlayerEvent)
    if args.player:
        player = (
            db.query(Player).filter(Player.discord_id == str(args.player)).first()
        )
        if player is None:
            print(f"no player with discord_id {args.player}")
            return
        q = q.filter(PlayerEvent.player_id == player.id)
    if args.min_importance:
        q = q.filter(PlayerEvent.importance >= args.min_importance)

    rows = q.order_by(PlayerEvent.id.desc()).limit(args.limit).all()
    rule(f"PLAYER EVENTS ({len(rows)})")
    for e in rows:
        print(f"  #{e.id:<6} p{e.player_id:<5} imp{e.importance} "
              f"{e.kind:<12} {(e.content or '')[:80]}")


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

def cmd_purge_deflected(db, args) -> None:
    """
    Guard deflections are stored for the audit trail and already excluded from
    context. Once you have seen the pattern, they are just rows.
    """
    rule("PURGE DEFLECTED TURNS")
    cutoff = datetime.utcnow() - timedelta(days=args.older_than_days)

    victims = []
    for m in db.query(Message).filter(Message.meta.isnot(None)):
        if not (isinstance(m.meta, dict) and m.meta.get("deflected")):
            continue
        if m.created_at and m.created_at > cutoff:
            continue
        victims.append(m)

    what = f"deflected turns older than {args.older_than_days}d"
    if not confirm(args, what, len(victims)):
        for m in victims[:10]:
            print(f"    #{m.id} {m.role}: {(m.content or '')[:70]}")
        if len(victims) > 10:
            print(f"    ... and {len(victims) - 10} more")
        return

    for m in victims:
        db.delete(m)
    db.commit()
    print(f"  done: {human(len(victims))} removed")


def cmd_prune_events(db, args) -> None:
    """Drop old, low-importance player events that no dossier needs."""
    rule("PRUNE PLAYER EVENTS")
    cutoff = datetime.utcnow() - timedelta(days=args.older_than_days)

    q = (
        db.query(PlayerEvent)
        .filter(PlayerEvent.importance <= args.max_importance)
        .filter(PlayerEvent.created_at < cutoff)
    )
    if not args.include_uncompacted:
        # Only prune what has already been folded into a dossier, so nothing
        # is lost that the player's history has not absorbed.
        folded = {
            pm.player_id: (pm.summarised_through_event_id or 0)
            for pm in db.query(PlayerMemory)
        }
        victims = [e for e in q.all() if e.id <= folded.get(e.player_id, 0)]
    else:
        victims = q.all()

    what = (f"events importance<={args.max_importance} older than "
            f"{args.older_than_days}d"
            + ("" if args.include_uncompacted else ", already folded into dossiers"))
    if not confirm(args, what, len(victims)):
        return

    for e in victims:
        db.delete(e)
    db.commit()
    print(f"  done: {human(len(victims))} removed")


def cmd_forget_conversation(db, args) -> None:
    """Erase one scene entirely: turns and summary."""
    rule("FORGET CONVERSATION")
    convo = _find_conversation(db, args.ident)
    if convo is None:
        print(f"  no conversation matching {args.ident!r}")
        return

    n = (
        db.query(func.count(Message.id))
        .filter(Message.conversation_id == convo.id)
        .scalar()
        or 0
    )
    print(f"  scene {convo.id} ({convo.source}:{convo.external_id})")
    if not confirm(args, f"all turns + summary for scene {convo.id}", n or 1):
        return

    db.query(Message).filter(Message.conversation_id == convo.id).delete()
    convo.world_state = {}
    db.commit()
    print(f"  done: {human(n)} turns removed, summary cleared")


def cmd_forget_player(db, args) -> None:
    """
    Erase one person's memory: dossier, standing, events.

    The Player row itself is kept so Discord identity mapping survives; pass
    --identity to remove that too.
    """
    rule("FORGET PLAYER")
    player = (
        db.query(Player).filter(Player.discord_id == str(args.discord_id)).first()
    )
    if player is None:
        print(f"  no player with discord_id {args.discord_id}")
        return

    n_events = (
        db.query(func.count(PlayerEvent.id))
        .filter(PlayerEvent.player_id == player.id)
        .scalar()
        or 0
    )
    print(f"  player {player.primary_handle or player.discord_id} (id {player.id})")
    print(f"  {human(n_events)} events, dossier present: "
          f"{bool(db.query(PlayerMemory).filter(PlayerMemory.player_id == player.id).first())}")

    if not confirm(args, f"memory for player {player.discord_id}", n_events or 1):
        return

    db.query(PlayerEvent).filter(PlayerEvent.player_id == player.id).delete()
    db.query(PlayerMemory).filter(PlayerMemory.player_id == player.id).delete()
    if args.identity:
        db.delete(player)
        print("  identity row removed as well")
    db.commit()
    print("  done")


def cmd_reset_summary(db, args) -> None:
    """
    Clear a scene summary and its watermark.

    Use when a summary has drifted or absorbed something wrong: the raw turns
    are untouched, so the next compaction rebuilds it from source.
    """
    rule("RESET SCENE SUMMARY")
    convo = _find_conversation(db, args.ident)
    if convo is None:
        print(f"  no conversation matching {args.ident!r}")
        return
    if not confirm(args, f"summary for scene {convo.id} (turns kept)", 1):
        return
    convo.world_state = {}
    db.commit()
    print("  done: summary cleared, raw turns intact, will recompact on next activity")


def cmd_purge_bot_docs(db, args) -> None:
    """
    Drop world_documents authored by a bot.

    These are the RAG poison: the ingest script fed the daemon's own replies
    back in as canon, including 'I'm just a large language model'. Retrieval
    then serves that as established lore.

    NOTE: this removes the SQL rows only. The Qdrant vectors are a separate
    store and must be dropped there too, or retrieval will keep serving them.
    """
    rule("PURGE BOT-AUTHORED WORLD DOCUMENTS")
    q = db.query(WorldDocument).filter(
        or_(
            WorldDocument.body.like("[bot]%"),
            WorldDocument.body.ilike("%language model%"),
            WorldDocument.body.ilike("%as an AI%"),
        )
    )
    victims = q.all()

    for d in victims[:15]:
        print(f"    #{d.id} {(d.title or '')[:40]:<42} "
              f"{(d.body or '')[:60].replace(chr(10), ' ')}")
    if len(victims) > 15:
        print(f"    ... and {len(victims) - 15} more")

    if not confirm(args, "bot-authored / assistant-voice world documents",
                   len(victims)):
        return

    ids = [d.id for d in victims]
    for d in victims:
        db.delete(d)
    db.commit()
    print(f"  done: {human(len(ids))} SQL rows removed")
    print("  REMINDER: the matching Qdrant points are NOT removed by this tool.")
    print("  Until they are, retrieval still serves them. Doc ids:")
    print(f"  {ids[:50]}")


def cmd_vacuum(db, args) -> None:
    """Delete rows whose parent is gone."""
    rule("VACUUM ORPHANS")
    convo_ids = {c.id for c in db.query(Conversation.id)}
    player_ids = {p.id for p in db.query(Player.id)}

    orphan_msgs = [
        m for m in db.query(Message) if m.conversation_id not in convo_ids
    ]
    orphan_events = [
        e for e in db.query(PlayerEvent) if e.player_id not in player_ids
    ]
    orphan_mem = [
        p for p in db.query(PlayerMemory) if p.player_id not in player_ids
    ]

    total = len(orphan_msgs) + len(orphan_events) + len(orphan_mem)
    print(f"  orphaned messages      {len(orphan_msgs)}")
    print(f"  orphaned player events {len(orphan_events)}")
    print(f"  orphaned memory rows   {len(orphan_mem)}")

    if not confirm(args, "orphaned rows", total):
        return
    for row in orphan_msgs + orphan_events + orphan_mem:
        db.delete(row)
    db.commit()
    print(f"  done: {human(total)} removed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_tool",
        description="Inspect and clean GhostNet memory. Destructive commands "
                    "are dry-run until --apply is given.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def destructive(sp):
        sp.add_argument("--apply", action="store_true",
                        help="actually perform the deletion (default: dry run)")
        return sp

    sub.add_parser("stats", help="footprint of every memory store").set_defaults(
        func=cmd_stats)

    sp = sub.add_parser("conversations", help="largest scenes by stored turns")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_conversations)

    sp = sub.add_parser("conversation", help="inspect one scene")
    sp.add_argument("ident", help="channel id (external) or internal row id")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_conversation)

    sp = sub.add_parser("player", help="inspect one player's memory")
    sp.add_argument("discord_id")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_player)

    sp = sub.add_parser("events", help="list player events")
    sp.add_argument("--player", help="filter by discord id")
    sp.add_argument("--min-importance", type=int, default=0)
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_events)

    sp = destructive(sub.add_parser("purge-deflected",
                                    help="drop guard-deflected turns"))
    sp.add_argument("--older-than-days", type=int, default=7)
    sp.set_defaults(func=cmd_purge_deflected)

    sp = destructive(sub.add_parser("prune-events",
                                    help="drop old low-importance events"))
    sp.add_argument("--older-than-days", type=int, default=90)
    sp.add_argument("--max-importance", type=int, default=2)
    sp.add_argument("--include-uncompacted", action="store_true",
                    help="also prune events not yet folded into a dossier "
                         "(data loss: they are in no summary)")
    sp.set_defaults(func=cmd_prune_events)

    sp = destructive(sub.add_parser("forget-conversation",
                                    help="erase one scene"))
    sp.add_argument("ident")
    sp.set_defaults(func=cmd_forget_conversation)

    sp = destructive(sub.add_parser("forget-player",
                                    help="erase one person's memory"))
    sp.add_argument("discord_id")
    sp.add_argument("--identity", action="store_true",
                    help="also delete the Player row itself")
    sp.set_defaults(func=cmd_forget_player)

    sp = destructive(sub.add_parser("reset-summary",
                                    help="clear a scene summary, keep turns"))
    sp.add_argument("ident")
    sp.set_defaults(func=cmd_reset_summary)

    sp = destructive(sub.add_parser("purge-bot-docs",
                                    help="drop bot-authored world documents"))
    sp.set_defaults(func=cmd_purge_bot_docs)

    sp = destructive(sub.add_parser("vacuum", help="delete orphaned rows"))
    sp.set_defaults(func=cmd_vacuum)

    return p


def main() -> int:
    args = build_parser().parse_args()
    print(f"database: {settings.database_url.split('@')[-1]}")
    with SessionLocal() as db:
        args.func(db, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
