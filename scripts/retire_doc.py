#!/usr/bin/env python3
"""
Retire a world document: delete its vector, mark the row superseded.

Retrieval does NOT filter on status. `api/rag.py::search_similar` runs a bare
qc.search() with no query_filter and returns (text, score) pairs -- status is
written into the Qdrant payload and never read by anything. So flipping a row
to `superseded` in SQL accomplishes exactly nothing on its own: the vector
keeps competing, and the daemon keeps answering with it.

Deleting the point is the only thing that actually retires a document.

The SQL row is kept, not deleted. It is the record of what the world used to
say, and `_sources`-style provenance is worth more than the row costs.

    python scripts/retire_doc.py list                  # docs + vector counts
    python scripts/retire_doc.py retire --id 8         # dry run
    python scripts/retire_doc.py retire --id 8 --apply
    python scripts/retire_doc.py retire --title "GhostNet Daemon" --keep 21 --apply

Points carry `doc_id` in their payload but are created with a random UUID
(`PointStruct(id=str(uuid.uuid4()))`), and `WorldDocument.qdrant_point_id` is
never populated by the ingest path. So deletion goes by payload filter, not by
stored id -- and a document re-embedded twice has TWO points, both of which
this removes. `list` flags those.

Interacts with revector.py: that tool re-embeds any document missing a vector,
which would resurrect anything retired here. revector skips retired statuses
for exactly that reason.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client import models as qmodels  # noqa: E402
from sqlalchemy import select  # noqa: E402

from api.db import SessionLocal  # noqa: E402
from api.models import WorldDocument  # noqa: E402
from api.rag import upsert_world_documents  # noqa: E402
from api.settings import settings  # noqa: E402

# Anything in here is treated as "not live". Kept in sync with revector.py,
# which must refuse to re-embed these or retirement silently undoes itself.
RETIRED_STATUS = {"superseded", "retired", "deprecated", "retconned"}


def point_counts(qc: QdrantClient) -> Counter:
    """How many vectors exist per doc_id. More than one means a double embed."""
    counts: Counter = Counter()
    offset = None
    while True:
        points, offset = qc.scroll(
            collection_name=settings.collection,
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            counts[(p.payload or {}).get("doc_id")] += 1
        if offset is None:
            return counts


def cmd_list(args) -> int:
    qc = QdrantClient(url=settings.qdrant_url)
    counts = point_counts(qc)

    with SessionLocal() as db:
        docs = list(db.scalars(select(WorldDocument).order_by(WorldDocument.id)))

    print(f"\n  {len(docs)} document(s) in SQL, "
          f"{sum(counts.values())} point(s) in Qdrant\n")
    print(f"    {'id':<5} {'vec':<4} {'status':<12} {'created_by':<16} title")

    titles: dict = {}
    for d in docs:
        n = counts.get(d.id, 0)
        mark = "  <- NO VECTOR" if n == 0 else ("  <- DOUBLE" if n > 1 else "")
        print(f"    {d.id:<5} {n:<4} {(d.status or '?'):<12} "
              f"{(d.created_by or '?')[:16]:<16} {str(d.title)[:40]}{mark}")
        titles.setdefault((d.title or "").strip().lower(), []).append(d.id)

    # A shared title only matters if more than one copy still has a vector.
    # Once the loser is retired the row remains, deliberately -- reporting
    # that as a live duplicate would say the problem persists when it does not.
    dupes = {t: ids for t, ids in titles.items() if len(ids) > 1}
    contested = {t: ids for t, ids in dupes.items()
                 if sum(1 for i in ids if counts.get(i, 0)) > 1}
    settled = {t: ids for t, ids in dupes.items() if t not in contested}

    if contested:
        print(f"\n  {len(contested)} CONTESTED TITLE(S) -- more than one copy "
              f"retrieves, and whichever scores higher wins the turn:")
        for t, ids in contested.items():
            print(f"    {ids}  {t}")
        print("    retire the losers: retire_doc.py retire --title <t> "
              "--keep <id> --apply")

    if settled:
        print(f"\n  {len(settled)} shared title(s), already resolved "
              f"(one vector each, rows kept as history):")
        for t, ids in settled.items():
            held = [i for i in ids if counts.get(i, 0)]
            print(f"    {ids} -> retrieves as {held}  {t}")

    orphaned = [d.id for d in docs if counts.get(d.id, 0) == 0
                and (d.status or "").lower() not in RETIRED_STATUS]
    if orphaned:
        print(f"\n  {len(orphaned)} active document(s) have no vector and are "
              f"invisible to retrieval: {orphaned}")
        print("    fix with: python scripts/revector.py --apply")

    stray = [k for k in counts if k is None or
             k not in {d.id for d in docs}]
    if stray:
        print(f"\n  {sum(counts[k] for k in stray)} point(s) have no matching "
              f"SQL row (doc_id {stray}).")

    return 0


def resolve(db, args) -> list:
    """Documents matching --id / --title, minus anything in --keep."""
    q = select(WorldDocument)
    if args.id is not None:
        q = q.where(WorldDocument.id == args.id)
    targets = list(db.scalars(q.order_by(WorldDocument.id)))

    if args.title:
        want = args.title.strip().lower()
        targets = [d for d in targets
                   if (d.title or "").strip().lower() == want]

    if getattr(args, "keep", None):
        targets = [d for d in targets if d.id not in args.keep]

    return targets


def cmd_retire(args) -> int:
    if args.id is None and not args.title:
        sys.exit("give --id or --title")

    qc = QdrantClient(url=settings.qdrant_url)
    counts = point_counts(qc)

    with SessionLocal() as db:
        targets = resolve(db, args)

        if not targets:
            sys.exit("no document matched -- nothing to retire")

        print(f"\n  retiring {len(targets)} document(s) "
              f"-> status={args.status}\n")
        for d in targets:
            print(f"    #{d.id:<4} {(d.status or '?'):<10} "
                  f"{(d.created_by or '?')[:16]:<16} {str(d.title)[:44]}")
            print(f"           {counts.get(d.id, 0)} vector(s) to delete")
            print(f"           {(d.body or '')[:150].strip()}...")
            print()

        if args.keep:
            kept = [d.id for d in db.scalars(select(WorldDocument))
                    if d.id in args.keep]
            print(f"  keeping: {kept}\n")

        if not args.apply:
            print("  DRY RUN -- nothing changed. Re-run with --apply.")
            return 0

        # Capture ids while the session is still open. Reading d.id after the
        # `with` block exits raises DetachedInstanceError -- commit expires the
        # instances, and the refresh needs a session that no longer exists.
        target_ids = [d.id for d in targets]

        deleted = 0
        for d in targets:
            if counts.get(d.id, 0):
                qc.delete(
                    collection_name=settings.collection,
                    points_selector=qmodels.FilterSelector(
                        filter=qmodels.Filter(must=[
                            qmodels.FieldCondition(
                                key="doc_id",
                                match=qmodels.MatchValue(value=d.id),
                            )
                        ])
                    ),
                )
                deleted += counts[d.id]
            d.status = args.status
            d.qdrant_point_id = None
        db.commit()

    after = point_counts(qc)
    still = [i for i in target_ids if after.get(i, 0)]
    print(f"  deleted {deleted} point(s); "
          f"{len(target_ids)} row(s) marked {args.status}")
    if still:
        print(f"  WARNING: still vectored: {still}")
        return 1
    print("  verified: no vectors remain for the retired document(s)")
    return 0


async def _restore(args, qc: QdrantClient) -> int:
    """
    Put a retired document back: re-embed it AND return it to active.

    Both halves are required. Re-embedding alone leaves a `superseded` row
    holding a live vector -- and because retrieval ignores status, that
    document would answer players while every audit reported it as retired.
    Restoring status alone does nothing at all.

    Scoped to one document on purpose. `revector.py --include-retired` is the
    blunt instrument and brings back everything.
    """
    counts = point_counts(qc)

    with SessionLocal() as db:
        targets = resolve(db, args)
        if not targets:
            sys.exit("no document matched -- nothing to restore")

        target_ids = [d.id for d in targets]

        print(f"\n  restoring {len(targets)} document(s) "
              f"-> status={args.status}\n")
        for d in targets:
            print(f"    #{d.id:<4} {(d.status or '?'):<12} "
                  f"{(d.created_by or '?')[:16]:<16} {str(d.title)[:44]}")
            print(f"           {counts.get(d.id, 0)} vector(s) now; "
                  f"re-embedding costs ~2.5s")
            print(f"           {(d.body or '')[:150].strip()}...")
            print()

        already = [d.id for d in targets if counts.get(d.id, 0)]
        if already:
            print(f"  NOTE: {already} already have a vector. Re-embedding "
                  f"adds a SECOND point rather than replacing it, because "
                  f"points get a random uuid. Retire first, then restore.")
            if not args.apply:
                pass
            else:
                sys.exit("refusing to double-embed -- retire it first")

        if not args.apply:
            print("  DRY RUN -- nothing changed. Re-run with --apply.")
            return 0

        for d in targets:
            d.status = args.status

        async with httpx.AsyncClient(timeout=120) as http:
            await upsert_world_documents(http=http, qc=qc, db=db, docs=targets)

    after = point_counts(qc)
    missing = [i for i in target_ids if not after.get(i, 0)]
    print(f"  restored {len(target_ids)} document(s) to {args.status}")
    if missing:
        print(f"  WARNING: still unvectored: {missing}")
        return 1
    print("  verified: vectors present and status active")
    return 0


def cmd_restore(args) -> int:
    if args.id is None and not args.title:
        sys.exit("give --id or --title")
    return asyncio.run(_restore(args, QdrantClient(url=settings.qdrant_url)))


def main() -> int:
    p = argparse.ArgumentParser(
        prog="retire_doc",
        description="Delete a world document's vector and mark it superseded. "
                    "Dry run unless --apply.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="documents with vector counts")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("retire", help="retire by id or title")
    sp.add_argument("--id", type=int)
    sp.add_argument("--title", help="exact title, case-insensitive")
    sp.add_argument("--keep", type=int, nargs="*", default=[],
                    help="ids to spare (for retiring all but one of a "
                         "duplicate title)")
    sp.add_argument("--status", default="superseded",
                    help="status to write (default: superseded)")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_retire)

    sp = sub.add_parser("restore", help="re-embed a retired document and "
                                        "return it to active")
    sp.add_argument("--id", type=int)
    sp.add_argument("--title", help="exact title, case-insensitive")
    sp.add_argument("--status", default="active",
                    help="status to write (default: active)")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_restore)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
