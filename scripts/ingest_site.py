#!/usr/bin/env python3
"""
Pull what strangers typed into the website's console, and screen it.

This is the site-to-world half of the leak loop. overworldnex.us is on
different hosting that this box cannot reach except over HTTPS, so the VM
initiates every crossing: it asks feed.php for everything after its
watermark, screens each message, and stores it.

    python scripts/ingest_site.py status          # watermark + what is held
    python scripts/ingest_site.py pull            # dry run
    python scripts/ingest_site.py pull --apply
    python scripts/ingest_site.py review          # what is waiting to be used
    python scripts/ingest_site.py pressure        # ambient traffic, no text

WHAT THIS DELIBERATELY DOES NOT DO

It does not embed anything, create WorldDocuments, or write WorldEvents. The
corpus already survived one poisoning -- twelve bot-authored self-quotes in
Qdrant, including "I'm just a large language model", scoring alongside curated
canon and indistinguishable from it. That came from the daemon quoting itself.
This channel is strictly worse: unauthenticated text from the open internet,
from people with no stake in the world, arriving on exactly the surface a
prompt injection would use.

So signals land in site_signals and stop there. Promotion into world material
is a separate, deliberate act with a human deciding. Seed a ruling, never the
deliberation.

THE WATERMARK

MAX(remote_id), not stored cursor state. remote_id is unique, so re-running is
harmless and a partial failure just resumes. Nothing to drift out of sync, and
replaying a range is `--since N`.

Configure in /etc/default/ghostnet-api (the API ignores .env):

    SITE_FEED_URL=https://overworldnex.us/admin/feed.php
    SITE_FEED_KEY=<the feed_key from the site's ghost_config.php>
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from api.db import SessionLocal, utcnow  # noqa: E402
from api.guard import screen_input  # noqa: E402
from api.models import SiteSignal  # noqa: E402
from api.settings import settings  # noqa: E402


def _require_config() -> tuple:
    url = (settings.site_feed_url or "").strip()
    key = (settings.site_feed_key or "").strip()
    if not url or not key:
        # Exit 0, not 1. This runs on a timer, and "the site is not wired up
        # yet" is a state rather than a failure -- exiting non-zero would put
        # a failed unit in the journal every ten minutes until deployment.
        print("  SITE_FEED_URL / SITE_FEED_KEY not set -- nothing to do.")
        print("  Set them in /etc/default/ghostnet-api to enable the loop.")
        raise SystemExit(0)
    low = url.lower()
    loopback = low.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
    if not low.startswith("https://") and not loopback:
        # The key grants read access to everything visitors have typed. Over
        # plain HTTP it travels in clear on every poll, to be replayed by
        # anyone on the path. Loopback is exempted because the test harness
        # runs the whole site stack in containers on this box, and nothing
        # there leaves the machine.
        sys.exit(f"refusing to send the feed key over a non-HTTPS URL: {url}")
    return url, key


def _parse_ts(value) -> datetime:
    """The site sends MySQL DATETIME in UTC."""
    if not value:
        return utcnow()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return utcnow()


def watermark(db) -> int:
    return int(db.scalar(select(func.coalesce(func.max(SiteSignal.remote_id), 0))) or 0)


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def cmd_pull(args) -> int:
    url, key = _require_config()

    with SessionLocal() as db:
        since = args.since if args.since is not None else watermark(db)
        start = since
        fetched, stored = [], 0
        counts: Counter = Counter()
        pages = 0

        with httpx.Client(timeout=settings.site_http_timeout) as http:
            while True:
                pages += 1
                try:
                    r = http.get(
                        url,
                        params={"since": since, "limit": settings.site_ingest_limit},
                        headers={"X-Ghost-Key": key},
                    )
                except httpx.HTTPError as e:
                    print(f"  request failed: {e!r}")
                    return 1

                if r.status_code == 403:
                    print("  403 Forbidden -- SITE_FEED_KEY does not match the "
                          "site's feed_key")
                    return 1
                if r.status_code >= 400:
                    print(f"  HTTP {r.status_code}: {r.text[:200]}")
                    return 1

                try:
                    data = r.json()
                except ValueError:
                    print(f"  response was not JSON: {r.text[:200]}")
                    return 1
                if not data.get("ok"):
                    print(f"  feed refused: {data.get('error')}")
                    return 1

                rows = data.get("rows") or []
                fetched.extend(rows)
                since = int(data.get("last_id") or since)

                if not data.get("more") or pages >= args.max_pages:
                    break

        print(f"\n  watermark {start} -> {since}   "
              f"{len(fetched)} row(s) in {pages} request(s)\n")

        if not fetched:
            print("  nothing new")
            return 0

        # Screen before storing. The verdict is recorded rather than used to
        # drop anything: what strangers try on the console is itself worth
        # seeing, and a rash of injection attempts is something to notice.
        seen = {
            r_id for (r_id,) in db.execute(
                select(SiteSignal.remote_id).where(
                    SiteSignal.remote_id.in_([int(x["id"]) for x in fetched])
                )
            )
        }

        for row in fetched:
            rid = int(row["id"])
            msg = (row.get("message") or "").strip()
            verdict = screen_input(msg)
            category = verdict.category if verdict.blocked else "clean"
            counts[category] += 1

            dup = rid in seen
            if dup:
                counts["_duplicate"] += 1

            flag = "" if category == "clean" else f"  [{category}]"
            dupmark = "  (already held)" if dup else ""
            print(f"    #{rid:<5} {row.get('client', '?'):<14} "
                  f"{(row.get('page') or '-')[:18]:<18} "
                  f"{msg[:46]!r}{flag}{dupmark}")

            if dup or not args.apply:
                continue

            db.add(SiteSignal(
                remote_id=rid,
                occurred_at=_parse_ts(row.get("ts")),
                visitor=(row.get("visitor") or "")[:16],
                client=(row.get("client") or "")[:24],
                page=(row.get("page") or None),
                message=msg,
                screened=category,
                status="new",
            ))
            stored += 1

        print()
        for cat, n in counts.most_common():
            if cat != "_duplicate":
                print(f"    {cat:<14} {n}")
        if counts["_duplicate"]:
            print(f"    {'(duplicates)':<14} {counts['_duplicate']} already held")

        if not args.apply:
            print("\n  DRY RUN -- nothing written. Re-run with --apply.")
            return 0

        db.commit()
        print(f"\n  stored {stored} new signal(s); watermark now {watermark(db)}")
        print("  Nothing was embedded, and no world state changed. "
              "Promote with `review`.")
    return 0


# ---------------------------------------------------------------------------
# status / review / pressure
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    with SessionLocal() as db:
        total = db.scalar(select(func.count(SiteSignal.id))) or 0
        print(f"\n  watermark (max remote_id) : {watermark(db)}")
        print(f"  signals held              : {total}")
        if not total:
            return 0
        for label, col in (("status", SiteSignal.status), ("screened", SiteSignal.screened)):
            print(f"\n  by {label}:")
            for value, n in db.execute(
                select(col, func.count()).group_by(col).order_by(func.count().desc())
            ):
                print(f"    {str(value):<14} {n}")
        newest = db.scalar(select(func.max(SiteSignal.occurred_at)))
        print(f"\n  most recent signal        : {newest}")
    return 0


def cmd_review(args) -> int:
    with SessionLocal() as db:
        q = select(SiteSignal).where(SiteSignal.status == args.status)
        if args.clean_only:
            q = q.where(SiteSignal.screened == "clean")
        rows = list(db.scalars(q.order_by(SiteSignal.remote_id.desc()).limit(args.limit)))

        if not rows:
            print(f"\n  nothing with status={args.status}")
            return 0

        print(f"\n  {len(rows)} signal(s), status={args.status}, newest first\n")
        for s in rows:
            flag = "" if s.screened == "clean" else f"  [{s.screened}]"
            print(f"    #{s.remote_id:<5} {str(s.occurred_at)[:16]}  "
                  f"{(s.page or '-')[:18]:<18} {s.visitor[:8]}  "
                  f"{s.message[:60]!r}{flag}")

        if args.mark:
            ids = [s.remote_id for s in rows]
            for s in rows:
                s.status = args.mark
            db.commit()
            print(f"\n  marked {len(ids)} signal(s) as {args.mark}")
        else:
            print("\n  read-only. Use --mark used|rejected to advance them.")
    return 0


def cmd_pressure(args) -> int:
    """
    Ambient traffic, with no message text.

    What the world wants from the site is mostly not what anyone said -- it is
    that somewhere drew attention. This is that, and it is deliberately the
    one view that carries no quotable content, so it is safe to act on
    automatically in a way the raw signals are not.
    """
    cutoff = utcnow() - timedelta(days=args.days)
    with SessionLocal() as db:
        rows = list(db.execute(
            select(SiteSignal.page, func.count().label("hits"),
                   func.count(func.distinct(SiteSignal.visitor)).label("who"))
            .where(SiteSignal.occurred_at >= cutoff)
            .group_by(SiteSignal.page)
            .order_by(func.count().desc())
        ))
        total = sum(r.hits for r in rows)
        visitors = db.scalar(
            select(func.count(func.distinct(SiteSignal.visitor)))
            .where(SiteSignal.occurred_at >= cutoff)
        ) or 0

        print(f"\n  last {args.days} day(s): {total} signal(s) from "
              f"{visitors} distinct visitor(s)\n")
        if not rows:
            return 0
        width = max(len(str(r.page or '-')) for r in rows)
        for r in rows:
            bar = "#" * min(40, r.hits)
            print(f"    {str(r.page or '-'):<{width}}  {r.hits:>4}  "
                  f"({r.who} visitor{'s' if r.who != 1 else ''})  {bar}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="ingest_site",
        description="Pull and screen console signals from the website. "
                    "Nothing is embedded and no world state changes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pull", help="fetch new signals")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--since", type=int, help="override the watermark (replay)")
    sp.add_argument("--max-pages", type=int, default=20)
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("status", help="watermark and counts")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("review", help="list held signals")
    sp.add_argument("--status", default="new", choices=["new", "used", "rejected"])
    sp.add_argument("--clean-only", action="store_true",
                    help="hide anything the guard flagged")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--mark", choices=["new", "used", "rejected"],
                    help="advance the listed signals to this status")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("pressure", help="traffic by page, no message text")
    sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(func=cmd_pressure)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
