#!/usr/bin/env python3
"""
Re-embed world_documents that have no vector in Qdrant.

Postgres is the durable store; Qdrant is a derived index. They drift apart
whenever the collection is recreated, or when the non-atomic dual-write in
`/world/docs` commits one side and not the other. When that happens the
documents still exist and are completely invisible to retrieval -- the daemon
answers "the archive holds nothing" about canon sitting right there in SQL.

That is exactly what happened on 2026-09-20: the collection was wiped to clear
bot-authored poison, and 15 curated canon documents (Eris, the Witch of
Despair, the Echo Project, Major Districts, M.A.I.D. suits...) lost their
vectors along with it.

Dry run by default; nothing is written until --apply.

    python scripts/revector.py              # what is orphaned
    python scripts/revector.py --apply      # re-embed it

Embedding runs through api.rag, so documents get the same nomic task prefixes
as everything else. Roughly 2.5s per document on this host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from api.db import SessionLocal  # noqa: E402
from api.models import WorldDocument  # noqa: E402
from api.rag import upsert_world_documents  # noqa: E402
from api.settings import settings  # noqa: E402

# Kept in sync with retire_doc.py. A document in one of these states is
# meant to have no vector; re-embedding it would put it back in play.
RETIRED_STATUS = {"superseded", "retired", "deprecated", "retconned"}


def vectored_doc_ids() -> set:
    """doc_ids currently present in the collection."""
    ids, cursor = set(), None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": False}
        if cursor:
            body["offset"] = cursor
        req = urllib.request.Request(
            f"{settings.qdrant_url.rstrip('/')}/collections/"
            f"{settings.collection}/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        res = json.loads(urllib.request.urlopen(req).read())["result"]
        for p in res["points"]:
            did = (p.get("payload") or {}).get("doc_id")
            if did is not None:
                ids.add(did)
        cursor = res.get("next_page_offset")
        if not cursor:
            break
    return ids


async def run(args) -> int:
    have = vectored_doc_ids()

    with SessionLocal() as db:
        docs = list(db.scalars(select(WorldDocument).order_by(WorldDocument.id)))
        orphans = [d for d in docs if d.id not in have]

        if args.status:
            orphans = [d for d in orphans if d.status == args.status]

        # A retired document has no vector BY DESIGN -- retire_doc.py deleted
        # it, because retrieval ignores status and deleting the point is the
        # only thing that actually takes a document out of play. Without this
        # guard, the next revector run re-embeds it and silently undoes the
        # retirement.
        retired = []
        if not args.include_retired:
            retired = [d for d in orphans
                       if (d.status or "").lower() in RETIRED_STATUS]
            orphans = [d for d in orphans
                       if (d.status or "").lower() not in RETIRED_STATUS]

        print(f"  world_documents in SQL : {len(docs)}")
        print(f"  doc_ids with vectors   : {len(have)}")
        print(f"  ORPHANED               : {len(orphans)}")
        if retired:
            print(f"  retired (left alone)   : {len(retired)} "
                  f"-> {[d.id for d in retired]}")
        print()

        if not orphans:
            print("  nothing to do -- every document is indexed")
            return 0

        for d in orphans:
            print(f"    #{d.id:<3} {d.kind or '?':<12} {str(d.title)[:58]!r}")

        if not args.apply:
            print(f"\n  DRY RUN -- re-run with --apply to embed "
                  f"{len(orphans)} document(s) "
                  f"(~{len(orphans) * 2.5 / 60:.1f} min)")
            return 0

        print(f"\n  embedding {len(orphans)} document(s)...")
        async with httpx.AsyncClient(timeout=120) as http:
            qc = QdrantClient(url=settings.qdrant_url)
            # Batched so a failure part-way leaves a known-good prefix rather
            # than an all-or-nothing gamble on a slow local embedder.
            for i in range(0, len(orphans), args.batch):
                chunk = orphans[i:i + args.batch]
                await upsert_world_documents(http=http, qc=qc, db=db, docs=chunk)
                print(f"    +{len(chunk)} (ids {[d.id for d in chunk]})")

    after = vectored_doc_ids()
    print(f"\n  done: {len(have)} -> {len(after)} vectored doc_ids")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="revector",
        description="Re-embed world_documents missing from Qdrant. "
                    "Dry run unless --apply.",
    )
    p.add_argument("--apply", action="store_true")
    p.add_argument("--batch", type=int, default=5)
    p.add_argument("--status", help="only documents with this status")
    p.add_argument("--include-retired", action="store_true",
                   help="also re-embed superseded/retired/deprecated "
                        "documents, putting them back into retrieval")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
