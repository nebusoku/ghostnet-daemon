#!/usr/bin/env python3
"""
Seed world canon from canon/*.json into Postgres + Qdrant.

Dry run by default. Nothing is written until --apply.

    python scripts/seed_canon.py plan                  # what would be seeded
    python scripts/seed_canon.py seed                  # dry run
    python scripts/seed_canon.py seed --apply          # write it
    python scripts/seed_canon.py seed --only foundation --apply

Tiers, in descending authority:

  canon/foundation.json  status=active    -- authored by you (website). Truth.
  canon/lore.json        status=active    -- authored Markdown, via import_lore.
  canon/tone.json                         -- voice and house style.
  canon/locations.json   status=proposed  -- derived from channel structure.
  canon/emergent.json    status=proposed  -- improvised in play, needs a ruling.
  canon/proposed.json    status=proposed  -- machine-woven drafts. OPT-IN ONLY:
                                             reachable with --only proposed,
                                             never by a bare `seed --apply`.

`proposed` entries are staged for you to rule on; several record explicit
contradictions rather than resolving them silently, because resolving
someone else's setting by guesswork is how the corpus got poisoned the
first time.

STATUS IS NOT A SAFETY BOUNDARY. `api/rag.py::search_similar` runs a bare
qc.search() with no query_filter, so a document seeded as `proposed`
competes for retrieval exactly like settled canon -- STUB text and all.
Do not seed anything here as a way of staging it. Staging happens in the
JSON files, before this script runs.

The one real protection is that this script REFUSES TO TRANSMIT gm-only
documents (see EXCLUDE_STATUS / EXCLUDE_TAGS). That refusal, not retrieval
filtering, is what keeps real-world influences out of the daemon's mouth.

To take a live document out of play, delete its vector:
    python scripts/retire_doc.py retire --id <n> --apply

Seeding goes through the API's /world/docs endpoint rather than writing the
database directly, so documents land in BOTH stores through the same path
the application uses.

/world/docs is INSERT-ONLY -- it never upserts on title. Re-seeding a tier
that is already seeded doubles every document in it, and both copies then
retrieve. A pre-flight check refuses to --apply when a title is already
live; override with --allow-duplicates only if you mean it.

(The collection was wiped and reseeded on 2026-09-20 to clear 12 bot-authored
self-quotes, including "I'm just a large language model". It is clean as of
2026-09-24: 27 documents, one vector each, no orphans. Verify any time with
`python scripts/retire_doc.py list`.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CANON_DIR = ROOT / "canon"

TIERS = ["foundation", "lore", "tone", "locations", "emergent"]

# Loadable only through an explicit --only. `weave_lore.py` tells you to run
# `seed --only proposed --apply`, which argparse rejected outright because
# "proposed" was not a valid choice -- and load() iterates TIERS, so the file
# would not have been read even if it had been. Kept out of TIERS rather than
# added to it: woven drafts are unreviewed by definition, and a bare
# `seed --apply` must never sweep them into the collection.
OPT_IN_TIERS = ["proposed"]

SELECTABLE = TIERS + OPT_IN_TIERS

# Documents that must NEVER reach the retrieval collection. GM reference
# naming real-world influences is the clear case: if the daemon can retrieve
# "this world draws on Ghost in the Shell", it will cite those works in-world.
EXCLUDE_STATUS = {"gm-only"}
EXCLUDE_TAGS = {"gm-only", "do-not-retrieve", "visibility:gm"}


def is_excluded(doc: dict) -> bool:
    if doc.get("status") in EXCLUDE_STATUS:
        return True
    return bool(EXCLUDE_TAGS & set(doc.get("tags") or []))


def backend() -> tuple:
    url = os.getenv("BACKEND_URL") or os.getenv("GHOSTNET_URL") or "http://127.0.0.1:8001"
    key = os.getenv("BACKEND_API_KEY") or os.getenv("API_KEY") or ""
    if not key:
        sys.exit("set API_KEY (or BACKEND_API_KEY) -- e.g. source /etc/default/ghostnet-api")
    return url.rstrip("/"), {"Authorization": f"Bearer {key}"}


def live_titles(url, headers) -> dict:
    """
    Titles already in the world corpus, lowercased -> [doc ids].

    /world/docs is INSERT-ONLY -- it never upserts on title, so re-seeding a
    tier that is already seeded silently doubles every document in it. Both
    copies then retrieve, and whichever scores higher on a given query is the
    one the daemon answers with. Nothing downstream detects this, so the
    check has to happen here.

    None if the corpus could not be read; the caller treats that as unknown
    rather than as "no duplicates".
    """
    try:
        r = requests.get(f"{url}/world/docs", headers=headers,
                         params={"limit": 1000}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"\n  WARNING: could not read live documents ({e}).")
        print("  Seeding without a duplicate check.")
        return None

    out: dict = {}
    for d in r.json():
        out.setdefault((d.get("title") or "").strip().lower(), []).append(d.get("id"))
    return out


def load(only=None) -> list:
    docs = []
    for tier in ([only] if only else TIERS):
        path = CANON_DIR / f"{tier}.json"
        if not path.is_file():
            print(f"  (missing {path.name}, skipping)")
            continue
        entries = json.loads(path.read_text(encoding="utf-8"))
        for e in entries:
            e["_tier"] = tier
            e["_excluded"] = is_excluded(e)
        docs.extend(entries)
    return docs


def cmd_plan(args) -> None:
    docs = load(args.only)
    if not docs:
        sys.exit("no canon files found")

    by_tier = Counter(d["_tier"] for d in docs)
    by_kind = Counter(d.get("kind", "?") for d in docs)
    by_status = Counter(d.get("status", "?") for d in docs)

    print(f"\n{len(docs)} canon documents\n")
    for label, counter in (("tier", by_tier), ("kind", by_kind), ("status", by_status)):
        print(f"  by {label}:")
        for k, n in counter.most_common():
            print(f"    {k:<14} {n:>3}")
        print()

    excluded = [d for d in docs if d["_excluded"]]
    if excluded:
        print(f"  {len(excluded)} document(s) are GM-ONLY and will NOT be seeded:")
        for d in excluded:
            print("    - " + str(d.get("title")))
        print()

    conflicts = [d for d in docs if "CONFLICT" in d.get("body", "")
                 or "conflict" in (d.get("tags") or [])]
    if conflicts:
        print(f"  {len(conflicts)} document(s) record an UNRESOLVED CONTRADICTION.")
        print("  These are staged as `proposed` and need your ruling:")
        for d in conflicts:
            print(f"    - {d.get('title')}")
        print()

    stubs = [d for d in docs if "STUB" in d.get("body", "")]
    if stubs:
        print(f"  {len(stubs)} stub(s) need authoring before they are useful:")
        for d in stubs:
            print(f"    - {d.get('title')}")
        print()

    chars = sum(len(d.get("body", "")) for d in docs)
    print(f"  total body text: {chars:,} chars "
          f"(~{chars // 4:,} tokens to embed, ~{len(docs) * 2.5 / 60:.1f} min at 2.5s/doc)")


def cmd_seed(args) -> None:
    url, headers = backend()
    docs = load(args.only)
    if not docs:
        sys.exit("no canon files found")

    if args.wipe_first:
        print("\n  --wipe-first given.")
        print("  This tool does NOT delete anything. Recreate the collection yourself:")
        print("    curl -X DELETE localhost:6333/collections/ghostnet_docs")
        print("    curl -X PUT localhost:6333/collections/ghostnet_docs \\")
        print("      -H 'Content-Type: application/json' \\")
        print("      -d '{\"vectors\":{\"size\":768,\"distance\":\"Cosine\"}}'")
        print("  and clear world_documents in SQL if you want a matching reset.\n")
        return

    held_back = [d for d in docs if d["_excluded"]]
    docs = [d for d in docs if not d["_excluded"]]
    payload = [{k: v for k, v in d.items() if not k.startswith("_")} for d in docs]

    if held_back:
        print(f"\n  WITHHELD from retrieval ({len(held_back)}):")
        for d in held_back:
            print("    - " + str(d.get("title")))

    print(f"\n  backend: {url}")
    print(f"  documents: {len(payload)}")
    for d in docs:
        print(f"    [{d['_tier']:<10}] {d.get('status','?'):<9} {d.get('title')}")

    live = live_titles(url, headers)
    if live:
        clashes = [(d, live[(d.get("title") or "").strip().lower()])
                   for d in docs
                   if (d.get("title") or "").strip().lower() in live]
        if clashes:
            print(f"\n  {len(clashes)} TITLE(S) ARE ALREADY LIVE. Seeding "
                  f"inserts a second copy rather than updating:")
            for d, ids in clashes:
                print(f"    doc{ids}  [{d['_tier']}] {d.get('title')}")
            print("\n  Retire the live copy first:")
            print(f"    python scripts/retire_doc.py retire --id "
                  f"{clashes[0][1][0]} --apply")
            print("  or re-run with --allow-duplicates to seed anyway.")
            if args.apply and not args.allow_duplicates:
                sys.exit(1)

    if not args.apply:
        print("\n  DRY RUN -- nothing sent. Re-run with --apply to seed.")
        return

    # Small batches: each document costs an embedding round-trip (~2.5s),
    # so a large batch can exceed the request timeout.
    sent = 0
    ids = []
    for i in range(0, len(payload), args.batch):
        chunk = payload[i:i + args.batch]
        print(f"\n  sending batch {i // args.batch + 1} ({len(chunk)} docs)...")
        r = requests.post(f"{url}/world/docs", json=chunk, headers=headers,
                          timeout=max(60, 30 * len(chunk)))
        if r.status_code >= 400:
            print(f"  FAILED {r.status_code}: {r.text[:300]}")
            sys.exit(1)
        data = r.json()
        sent += data.get("inserted", 0)
        ids.extend(data.get("ids", []))
        print(f"    inserted {data.get('inserted')} -> ids {data.get('ids')}")

    print(f"\n  seeded {sent} document(s). ids: {ids}")
    print("  Verify retrieval:")
    print(f"    curl -s {url}/search -H 'Authorization: Bearer <key>' \\")
    print("      -H 'Content-Type: application/json' \\")
    print("      -d '{\"query\":\"who are the factions\",\"top_k\":3}'")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="seed_canon",
        description="Seed world canon. Dry run unless --apply.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="summarise what would be seeded")
    sp.add_argument("--only", choices=SELECTABLE)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("seed", help="send canon to the API")
    sp.add_argument("--only", choices=SELECTABLE)
    sp.add_argument("--apply", action="store_true", help="actually write")
    sp.add_argument("--batch", type=int, default=5)
    sp.add_argument("--allow-duplicates", action="store_true",
                    help="seed even if a title is already live")
    sp.add_argument("--wipe-first", action="store_true",
                    help="print the commands to reset the collection first")
    sp.set_defaults(func=cmd_seed)

    args = p.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
