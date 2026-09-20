#!/usr/bin/env python3
"""
Export Discord channel history to local JSONL. Phase 1 of two.

    export  -->  archive.jsonl  -->  inspect  -->  ingest (separate script)

This script ONLY reads Discord and writes files. It never calls the GhostNet
API, never touches Postgres, and never writes to Qdrant. That separation is
deliberate: the previous importer
(`discord_ingest_general_to_world_docs.py`) piped Discord straight into
/world/docs with no filtering, which is how 12 bot-authored documents --
including a verbatim "I'm just a large language model" -- became retrievable
canon. Exporting first means the archive is a re-ingestable source of truth
you can filter differently later without re-hitting Discord.

Nothing is filtered at export time, on purpose. Bot messages are captured and
flagged (`author.bot`), so the ingest step can decide. Throwing data away here
would mean another export to get it back.

Uses the REST API directly rather than discord.py's gateway: no second gateway
session alongside the running bot, no presence flap, no intent requirements,
and clean resumability.

Usage
-----
    export DISCORD_TOKEN=...            # or rely on .env / systemd env

    python scripts/discord_export.py channels --guild <guild_id>
    python scripts/discord_export.py export --channel <channel_id>
    python scripts/discord_export.py export --guild <guild_id> --all
    python scripts/discord_export.py stats exports/

Exports are incremental: re-running only fetches messages newer than the
highest id already archived for that channel.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import requests

API = "https://discord.com/api/v10"
DEFAULT_OUT = Path("exports")
PAGE = 100  # Discord's max per request


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------

def load_token() -> str:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        # Fall back to a .env beside the repo, matching how the bot is run.
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.is_file():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("DISCORD_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip("'\"")
                    break
    if not token:
        sys.exit("DISCORD_TOKEN not set (env or .env)")
    return token


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "GhostNetExporter (overworldnex.us, 1.0)",
    }


# ---------------------------------------------------------------------------
# rate-limited GET
# ---------------------------------------------------------------------------

def get(url: str, token: str, params: Optional[dict] = None, tries: int = 6):
    """
    GET with Discord rate-limit handling.

    429s carry retry_after in the body. We also pace off the
    X-RateLimit-Remaining header so a long export does not walk into a
    global limit, which would affect the live bot on the same token.
    """
    for attempt in range(tries):
        r = requests.get(url, headers=headers(token), params=params, timeout=30)

        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 1.0)) if r.content else 1.0
            print(f"    rate limited, sleeping {wait:.1f}s", flush=True)
            time.sleep(wait + 0.25)
            continue

        if r.status_code in (500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"    {r.status_code} from Discord, retry in {wait}s", flush=True)
            time.sleep(wait)
            continue

        if r.status_code == 403:
            raise PermissionError(
                f"403 for {url} -- the bot lacks View Channel / Read Message History"
            )
        if r.status_code == 401:
            raise PermissionError("401 -- DISCORD_TOKEN is invalid")

        r.raise_for_status()

        # Stay well clear of the bucket limit; the live bot shares this token.
        remaining = r.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) <= 1:
            reset_after = float(r.headers.get("X-RateLimit-Reset-After", 1.0))
            time.sleep(reset_after + 0.1)

        return r.json()

    raise RuntimeError(f"gave up on {url} after {tries} attempts")


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------

TEXTLIKE = {0, 5, 10, 11, 12, 15}  # text, announcement, threads, forum


def list_channels(token: str, guild_id: str) -> List[dict]:
    chans = get(f"{API}/guilds/{guild_id}/channels", token)
    return [c for c in chans if c.get("type") in TEXTLIKE]


def cmd_channels(args) -> None:
    token = load_token()
    chans = list_channels(token, args.guild)
    print(f"{len(chans)} text-like channels in guild {args.guild}\n")
    print(f"  {'id':<22} {'type':>4}  name")
    for c in sorted(chans, key=lambda x: x.get("position", 0)):
        print(f"  {c['id']:<22} {c.get('type'):>4}  #{c.get('name')}")
    print("\nExport one:   python scripts/discord_export.py export --channel <id>")
    print("Export all:   python scripts/discord_export.py export --guild "
          f"{args.guild} --all")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _out_path(out_dir: Path, channel: dict) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                   for ch in (channel.get("name") or "channel"))
    return out_dir / f"{safe}-{channel['id']}.jsonl"


def _highest_id(path: Path) -> Optional[str]:
    """Largest message id already archived (snowflakes sort numerically)."""
    if not path.is_file():
        return None
    best = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                mid = int(json.loads(line)["id"])
            except Exception:
                continue
            best = max(best, mid)
    return str(best) if best else None


def fetch_messages(token: str, channel_id: str,
                   after: Optional[str]) -> Iterator[dict]:
    """
    Yield messages oldest-first.

    With `after` we page forward from a known point (incremental re-run).
    Without it we page backward from newest to the beginning of the channel,
    then reverse -- Discord only supports backward paging from the end.
    """
    if after:
        cursor = after
        while True:
            batch = get(f"{API}/channels/{channel_id}/messages", token,
                        {"limit": PAGE, "after": cursor})
            if not batch:
                return
            batch.sort(key=lambda m: int(m["id"]))   # API returns newest-first
            for m in batch:
                yield m
            cursor = batch[-1]["id"]
            if len(batch) < PAGE:
                return
    else:
        collected: List[dict] = []
        before = None
        while True:
            params = {"limit": PAGE}
            if before:
                params["before"] = before
            batch = get(f"{API}/channels/{channel_id}/messages", token, params)
            if not batch:
                break
            collected.extend(batch)
            before = batch[-1]["id"]
            print(f"    fetched {len(collected)}...", flush=True)
            if len(batch) < PAGE:
                break
        collected.sort(key=lambda m: int(m["id"]))
        yield from collected


def slim(m: dict, channel: dict) -> dict:
    """
    Keep what a later ingest or player-memory import could need.

    author.bot is preserved rather than filtered so the ingest step can make
    that call -- and so we never have to re-export to change our minds.
    """
    author = m.get("author") or {}
    ref = m.get("message_reference") or {}
    return {
        "id": m.get("id"),
        "channel_id": channel.get("id"),
        "channel_name": channel.get("name"),
        "timestamp": m.get("timestamp"),
        "edited_timestamp": m.get("edited_timestamp"),
        "author_id": author.get("id"),
        "author_name": author.get("username"),
        "author_global_name": author.get("global_name"),
        "author_bot": bool(author.get("bot", False)),
        "author_webhook": m.get("webhook_id") is not None,
        "content": m.get("content") or "",
        "reply_to_id": ref.get("message_id"),
        "thread_id": (m.get("thread") or {}).get("id"),
        "mentions": [u.get("id") for u in (m.get("mentions") or [])],
        "attachments": len(m.get("attachments") or []),
        "embeds": len(m.get("embeds") or []),
        "reactions": sum(r.get("count", 0) for r in (m.get("reactions") or [])),
        "pinned": bool(m.get("pinned", False)),
        "type": m.get("type"),
    }


def export_channel(token: str, channel: dict, out_dir: Path,
                   resume: bool = True) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _out_path(out_dir, channel)
    after = _highest_id(path) if resume else None

    label = f"#{channel.get('name')} ({channel['id']})"
    print(f"  {label}" + (f"  [resuming after {after}]" if after else "  [full]"))

    written = 0
    try:
        with path.open("a", encoding="utf-8") as fh:
            for m in fetch_messages(token, channel["id"], after):
                fh.write(json.dumps(slim(m, channel), ensure_ascii=False) + "\n")
                written += 1
    except PermissionError as e:
        print(f"    SKIPPED: {e}")
        return 0

    print(f"    +{written} messages -> {path}")
    return written


def cmd_export(args) -> None:
    token = load_token()
    out_dir = Path(args.out)

    if args.channel:
        chan = get(f"{API}/channels/{args.channel}", token)
        channels = [chan]
    elif args.guild and args.all:
        channels = list_channels(token, args.guild)
    else:
        sys.exit("give --channel <id>, or --guild <id> --all")

    print(f"exporting {len(channels)} channel(s) to {out_dir}/\n")
    total = 0
    for c in channels:
        total += export_channel(token, c, out_dir, resume=not args.no_resume)

    print(f"\ndone: {total} new message(s) archived")
    print("\nNothing has been ingested. Inspect first:")
    print(f"    python scripts/discord_export.py stats {out_dir}")


# ---------------------------------------------------------------------------
# stats -- look before you ingest
# ---------------------------------------------------------------------------

def cmd_stats(args) -> None:
    """
    Summarise an export so you can see what ingesting it would actually do.

    The headline number is bot-authored share: that is what poisoned the
    corpus last time.
    """
    root = Path(args.path)
    files = sorted(root.glob("*.jsonl")) if root.is_dir() else [root]
    if not files:
        sys.exit(f"no .jsonl files under {root}")

    total = bots = webhooks = empty = short = 0
    authors: Counter = Counter()
    per_channel: Counter = Counter()
    leaks = 0
    oldest = newest = None

    leak_markers = ("language model", "as an ai", "i cannot assist",
                    "i'm just a", "openai", "chatgpt")

    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue

                total += 1
                per_channel[m.get("channel_name") or m.get("channel_id")] += 1
                content = (m.get("content") or "").strip()

                if m.get("author_bot"):
                    bots += 1
                if m.get("author_webhook"):
                    webhooks += 1
                if not content:
                    empty += 1
                elif len(content) < args.min_len:
                    short += 1

                name = m.get("author_global_name") or m.get("author_name") or "?"
                authors[f"{name} ({m.get('author_id')})"] += 1

                low = content.lower()
                if any(k in low for k in leak_markers):
                    leaks += 1

                ts = m.get("timestamp")
                if ts:
                    oldest = min(oldest, ts) if oldest else ts
                    newest = max(newest, ts) if newest else ts

    print(f"archive: {root}  ({len(files)} file(s))")
    print(f"\n  total messages      {total:>8,}")
    print(f"  bot-authored        {bots:>8,}  ({100*bots/total if total else 0:.1f}%)")
    print(f"  webhook (Tupperbox) {webhooks:>8,}")
    print(f"  empty content       {empty:>8,}")
    print(f"  shorter than {args.min_len:<3}    {short:>8,}")
    if oldest:
        print(f"\n  date range          {oldest[:10]} .. {newest[:10]}")

    print("\n  per channel:")
    for name, n in per_channel.most_common(15):
        print(f"    {str(name):<28} {n:>7,}")

    print("\n  top authors:")
    for who, n in authors.most_common(10):
        print(f"    {who:<44} {n:>7,}")

    human = total - bots - webhooks - empty - short
    print(f"\n  {'=' * 56}")
    print(f"  INGEST PREVIEW (humans only, >={args.min_len} chars): "
          f"~{max(0, human):,} documents")
    if leaks:
        print(f"  WARNING: {leaks:,} message(s) contain assistant-voice text")
        print("           (\"language model\", \"as an AI\", ...) -- these are")
        print("           exactly what poisoned the corpus last time.")
    # Embedding is ~2.5s/doc on this VM, one HTTP call per text.
    secs = max(0, human) * 2.5
    print(f"  Embedding {max(0, human):,} docs at ~2.5s each ≈ "
          f"{secs/3600:.1f} hours sequentially.")
    print(f"  {'=' * 56}")


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        prog="discord_export",
        description="Export Discord history to JSONL. Reads Discord, writes "
                    "files. Never ingests.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("channels", help="list text channels in a guild")
    sp.add_argument("--guild", required=True)
    sp.set_defaults(func=cmd_channels)

    sp = sub.add_parser("export", help="archive channel history to JSONL")
    sp.add_argument("--channel", help="single channel id")
    sp.add_argument("--guild", help="guild id (with --all)")
    sp.add_argument("--all", action="store_true", help="every text channel")
    sp.add_argument("--out", default=str(DEFAULT_OUT))
    sp.add_argument("--no-resume", action="store_true",
                    help="re-fetch from the beginning instead of resuming")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("stats", help="summarise an export before ingesting")
    sp.add_argument("path", nargs="?", default=str(DEFAULT_OUT))
    sp.add_argument("--min-len", type=int, default=40)
    sp.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
