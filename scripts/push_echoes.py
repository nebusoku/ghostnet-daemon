#!/usr/bin/env python3
"""
Push fragments of the world onto the website.

The world-to-site half of the leak loop. The VM sends echoes to echo.php,
which stores them and regenerates a static echoes.json that the hidden console
reads. If the VM goes quiet the echoes go stale, which reads as the mesh going
quiet rather than as a broken page.

    python scripts/push_echoes.py candidates --from canon
    python scripts/push_echoes.py push --from canon --count 5
    python scripts/push_echoes.py push --from canon --count 5 --apply
    python scripts/push_echoes.py live            # what the site is serving
    python scripts/push_echoes.py prune --apply   # drop expired rows

EVERY ECHO LANDS ON A PUBLIC PAGE. That is the constraint the sources are
chosen around.

  canon    Verbatim sentences from active world_documents. Safe by
           construction: the text is already authored, already reviewed, and
           the provenance names the document it came from. gm-only material
           was never seeded into the database at all, so it cannot be reached
           from here even by accident -- and `status == active` is checked
           anyway, so a retired document stays retired.

  events   WorldEvent headlines. These were designed for exactly this: one
           line, world-facing, already written to be read.

  signals  Things visitors typed, promoted by hand. REQUIRES status='used'
           AND screened='clean', because this is the one source where a
           stranger's text could reach a public page. Anything auto-promoted
           here would be an open defacement channel: type it into the
           console, watch it appear on the website. So promotion stays a
           human act -- `ingest_site.py review --mark used`.

Nothing is generated. An echo is a quotation with a pointer back to what it
came from, which is what makes a whisper on the website traceable to a thing
in the Nexus. Generated fragments are how the corpus got poisoned the first
time; if that changes, it should change deliberately and be reviewed.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from api.db import SessionLocal  # noqa: E402
from api.models import SiteSignal, WorldDocument, WorldEvent  # noqa: E402
from api.settings import settings  # noqa: E402

MIN_CHARS, MAX_CHARS = 60, 280

# Which documents may be quoted onto a public page at all.
#
# An allowlist, not a blocklist, and the reason is what the first dry run
# offered up as atmospheric whispers:
#
#   doc:2  "Out-of-character (OOC) / real-world content must be explicitly
#           marked"
#   doc:3  "If a user attempts sexual content involving minors, refuse and
#           shift to a brief safety response."
#
# The second is a moderation rule. Published as a mysterious fragment on the
# website it would be, at best, alarming. Rules and canon-governance
# documents share a retrieval pool with in-world lore, and no amount of
# per-sentence filtering makes that safe when the output is public.
#
# This deliberately under-selects. doc#7 is kind=canon but holds real in-world
# material (Core, Mesh, Operator Interface), and doc#12 is kind=mechanics but
# describes traversal in-world. Both are excluded. Losing two sources costs
# nothing; publishing an age-policy line costs a great deal. Override with
# --kinds when you have read what it would send.
IN_WORLD_KINDS = {
    "lore", "setting", "location", "faction", "npc", "entity",
    "tech", "concept", "world", "project", "event", "item", "org",
}

# Second layer, applied per sentence. Catches in-world-kinded documents that
# still contain a line about the machinery.
_SKIP = re.compile(
    r"\[(PROC|SIG|WARN|HIDDEN|DAEMON)\b"
    r"|^\s*(STUB|CONFLICT|NOTE|SAFETY|TONE|AVOID|DO NOT SEED)\b"
    r"|^[A-Z][A-Z \-]{6,}$"                      # all-caps section headings
    r"|\bplayers?\b|\busers?\b"                  # out-of-world vocabulary
    r"|world_documents|\bOOC\b|in-universe|in‑universe"
    r"|\bcanon\b|\bretrieval\b|\bprompt\b|\bdaemon voice\b"
    r"|\brefuse\b|safety response|\bminors?\b",
    re.IGNORECASE | re.MULTILINE,
)


def _require_config() -> tuple:
    url = (settings.site_echo_url or "").strip()
    key = (settings.site_feed_key or "").strip()
    if not url or not key:
        sys.exit(
            "SITE_ECHO_URL and SITE_FEED_KEY must be set.\n"
            "  Add them to /etc/default/ghostnet-api, then:\n"
            "    set -a && . /etc/default/ghostnet-api && set +a"
        )
    low = url.lower()
    loopback = low.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
    if not low.startswith("https://") and not loopback:
        sys.exit(f"refusing to send the feed key over a non-HTTPS URL: {url}")
    return url, key


def _sentences(body: str):
    for raw in re.split(r"(?<=[.!?])\s+|\n{2,}", body or ""):
        s = " ".join(raw.split())
        if not s or _SKIP.search(s):
            continue
        if MIN_CHARS <= len(s) <= MAX_CHARS:
            yield s


def gather(db, source: str, allowed_kinds=None) -> list:
    """Candidate echoes as (body, source, default_scope) triples."""
    allowed_kinds = allowed_kinds or IN_WORLD_KINDS
    out = []

    if source == "canon":
        docs = db.scalars(
            select(WorldDocument).where(WorldDocument.status == "active")
        )
        for d in docs:
            if (d.kind or "").lower() not in allowed_kinds:
                continue
            for s in _sentences(d.body or ""):
                out.append((s, f"doc:{d.id}", "any"))

    elif source == "events":
        for e in db.scalars(select(WorldEvent).order_by(WorldEvent.id.desc())):
            head = " ".join((e.headline or "").split())
            if head and len(head) <= MAX_CHARS:
                out.append((head, f"world_event:{e.id}", "any"))

    elif source == "signals":
        # The only source carrying text this system did not author. Both
        # filters are required: promoted by a human, and clean at the guard.
        rows = db.scalars(
            select(SiteSignal)
            .where(SiteSignal.status == "used")
            .where(SiteSignal.screened == "clean")
            .order_by(SiteSignal.remote_id.desc())
        )
        for s in rows:
            msg = " ".join((s.message or "").split())
            if msg and len(msg) <= MAX_CHARS:
                out.append((msg, f"signal:{s.remote_id}", "any"))

    return out


def _kinds(args) -> set:
    raw = getattr(args, "kinds", None)
    if not raw:
        return IN_WORLD_KINDS
    return {k.strip().lower() for k in raw.split(",") if k.strip()}


def cmd_candidates(args) -> int:
    with SessionLocal() as db:
        cands = gather(db, args.source, _kinds(args))
    print(f"\n  {len(cands)} candidate(s) from '{args.source}'\n")
    if not cands and args.source == "signals":
        print("  Nothing promoted. Signals must be marked used by hand:")
        print("    python scripts/ingest_site.py review --clean-only --mark used")
        return 0
    for body, src, _ in cands[: args.limit]:
        print(f"    {src:<16} {body[:96]}")
    if len(cands) > args.limit:
        print(f"    ... and {len(cands) - args.limit} more")
    return 0


def cmd_push(args) -> int:
    url, key = _require_config()

    with SessionLocal() as db:
        cands = gather(db, args.source, _kinds(args))

    if not cands:
        print(f"\n  nothing to send from '{args.source}'")
        return 0

    rng = random.Random(args.seed)
    rng.shuffle(cands)
    chosen = cands[: args.count]

    payload = []
    for body, src, scope in chosen:
        item = {
            "body": body,
            "scope": args.scope or scope,
            "weight": args.weight,
            "source": src,
        }
        # ttl_hours is omitted entirely for a permanent echo. Sending 0 would
        # be refused by echo.php rather than silently meaning "forever" --
        # that inversion was a real bug once.
        if not args.no_expiry:
            item["ttl_hours"] = args.ttl
        payload.append(item)

    life = "no expiry" if args.no_expiry else f"{args.ttl}h"
    print(f"\n  {len(payload)} echo(es) -> {url}   weight {args.weight}, {life}\n")
    for item in payload:
        print(f"    [{item['source']}] {item['body'][:92]}")

    if not args.apply:
        print("\n  DRY RUN -- nothing sent. Re-run with --apply.")
        return 0

    body = {"echoes": payload}
    if args.prune:
        body["prune"] = True

    try:
        r = httpx.post(url, json=body, headers={"X-Ghost-Key": key},
                       timeout=settings.site_http_timeout)
    except httpx.HTTPError as e:
        print(f"\n  request failed: {e!r}")
        return 1

    if r.status_code == 403:
        print("\n  403 Forbidden -- SITE_FEED_KEY does not match the site's feed_key")
        return 1
    if r.status_code >= 400:
        print(f"\n  HTTP {r.status_code}: {r.text[:200]}")
        return 1

    data = r.json()
    if not data.get("ok"):
        print(f"\n  refused: {data.get('error')}")
        return 1

    print(f"\n  inserted {data.get('inserted')}, skipped {data.get('skipped')}, "
          f"pruned {data.get('pruned')}, now publishing {data.get('published')}")
    if data.get("skipped"):
        print("  WARNING: the site dropped some echoes as malformed.")
    return 0


def cmd_prune(args) -> int:
    url, key = _require_config()
    if not args.apply:
        print("\n  DRY RUN -- would ask the site to drop expired echoes. "
              "Re-run with --apply.")
        return 0
    r = httpx.post(url, json={"prune": True}, headers={"X-Ghost-Key": key},
                   timeout=settings.site_http_timeout)
    if r.status_code >= 400:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        return 1
    data = r.json()
    print(f"\n  pruned {data.get('pruned')}, now publishing {data.get('published')}")
    return 0


def cmd_live(args) -> int:
    """
    What the site is actually serving. Reads the public echoes.json, with no
    key -- deliberately the same view a visitor's console gets.
    """
    url, _ = _require_config()
    feed = url.rsplit("/admin/", 1)[0] + "/echoes.json"
    try:
        r = httpx.get(feed, timeout=settings.site_http_timeout)
    except httpx.HTTPError as e:
        print(f"  request failed: {e!r}")
        return 1
    if r.status_code >= 400:
        print(f"  HTTP {r.status_code} from {feed}")
        return 1

    data = r.json()
    rows = data.get("echoes") or []
    print(f"\n  {feed}")
    print(f"  generated {data.get('generated')}   {len(rows)} live echo(es)\n")
    for e in rows[: args.limit]:
        print(f"    w{e.get('weight')}  [{e.get('source') or '-'}]  "
              f"{(e.get('body') or '')[:88]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="push_echoes",
        description="Push world fragments to the website. Dry run unless --apply.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def src(sp):
        sp.add_argument("--from", dest="source", default="canon",
                        choices=["canon", "events", "signals"])
        sp.add_argument("--kinds",
                        help="comma-separated document kinds to allow "
                             "(default: in-world kinds only)")

    sp = sub.add_parser("candidates", help="what could be echoed")
    src(sp)
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_candidates)

    sp = sub.add_parser("push", help="send echoes to the site")
    src(sp)
    sp.add_argument("--count", type=int, default=5)
    sp.add_argument("--weight", type=int, default=1)
    sp.add_argument("--ttl", type=int, default=72, help="hours (default 72)")
    sp.add_argument("--no-expiry", action="store_true",
                    help="omit ttl_hours so the echo persists")
    sp.add_argument("--scope", help="override the scope (default: any)")
    sp.add_argument("--seed", type=int, help="fix the random selection")
    sp.add_argument("--prune", action="store_true",
                    help="also drop expired rows in the same call")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("prune", help="drop expired echoes")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("live", help="what the site is serving right now")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_live)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
