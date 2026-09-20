#!/usr/bin/env python3
"""
Draft connective back-lore from what the world already records.

Retrieval is only as good as coverage. A question about Eris scores 0.791
because a document exists; a question about the Undercroft finds whatever is
vaguely adjacent, because nothing defines it. Canon references far more than
it defines -- that gap is what makes responses feel hit-and-miss.

This finds terms the canon NAMES but never DESCRIBES, and drafts a document
for each, grounded in the passages that mention it.

Two rules it will not break:

  1. GROUNDED. Every draft is written from real excerpts -- existing canon,
     recorded player events, scene summaries. Nothing is invented from a bare
     term. An ungrounded generator is just the hallucination loop with extra
     steps.
  2. PROPOSED, NEVER PUBLISHED. Output goes to a review file with
     status="proposed". Nothing reaches the retrieval corpus without a human
     ratifying it. This project has already been poisoned once by bot text
     feeding itself as canon; the gate is the whole point.

Runs on the LOCAL backend by default. There is no player waiting, so it costs
nothing at the paid provider -- and it finally gives ghostnet-rp a job it is
fast enough for.

    python scripts/weave_lore.py gaps                    # what is undefined
    python scripts/weave_lore.py draft --limit 5         # draft the top gaps
    python scripts/weave_lore.py draft --term "Undercroft"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from api.db import SessionLocal  # noqa: E402
from api.llm import LLMError, generate_with  # noqa: E402
from api.memory import get_summary  # noqa: E402
from api.models import Conversation, PlayerEvent, WorldDocument  # noqa: E402
from api.settings import settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "canon" / "proposed.json"

# Multi-word Capitalised phrases and ALLCAPS/hyphen-code terms.
_TERM = re.compile(
    r"\b(?:[A-Z][a-z]+(?:[-‑][A-Z][a-z]+)?(?:\s+(?:of\s+|the\s+)?[A-Z][a-z]+)*)\b"
    r"|\b[A-Z]{2,}(?:-\d+)?\b"
)

# Sentence starts, common words, and terms that are already the SUBJECT of a
# document would otherwise dominate the ranking.
_STOP = {
    "The", "A", "An", "This", "That", "These", "Those", "It", "They", "There",
    "Their", "Its", "His", "Her", "You", "Your", "We", "Our", "If", "When",
    "Where", "What", "Who", "Some", "Most", "Not", "No", "All", "Each", "Do",
    "Does", "Use", "Used", "One", "Two", "Three", "Both", "Other", "Others",
    "Status", "Keep", "Drop", "Prefer", "Avoid", "Treat", "Say", "Write",
    "Return", "Note", "Beyond", "Below", "Above", "Here", "Every", "Never",
    "Always", "Only", "Then", "Than", "And", "But", "For", "Nor", "Yet", "So",
    "In", "On", "At", "By", "To", "Of", "Overworld Nexus", "Overworld",
    "Nexus", "GhostNet", "GhostNet Daemon", "OOC", "IRL", "AI",
}


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def gather(db):
    """Everything the world currently records, as (label, text) passages."""
    passages = []
    for d in db.scalars(select(WorldDocument).order_by(WorldDocument.id)):
        passages.append((f"doc#{d.id} {d.title}", f"{d.title}\n{d.body}"))
    for convo in db.scalars(select(Conversation)):
        s = get_summary(convo)
        if s:
            passages.append((f"scene {convo.external_id}", s))
    for ev in db.scalars(select(PlayerEvent).order_by(PlayerEvent.id.desc()).limit(200)):
        passages.append((f"event#{ev.id}", ev.content or ""))
    return passages


def defined_titles(db) -> set:
    out = set()
    for d in db.scalars(select(WorldDocument)):
        t = _norm(d.title or "")
        if not t:
            continue
        out.add(t.lower())
        # "M.A.I.D. Suits (Mobile Assault Intervention Devices)" -> "m.a.i.d. suits"
        out.add(re.sub(r"\s*\(.*?\)", "", t).lower().strip())
        out.add(re.sub(r"^(the)\s+", "", t.lower()).strip())
    return out


def find_gaps(passages, defined) -> list:
    """Terms the canon names but never defines, ranked by how often."""
    counts, where = Counter(), {}
    for label, text in passages:
        for m in _TERM.finditer(text or ""):
            t = _norm(m.group(0))
            if len(t) < 4 or t in _STOP:
                continue
            low = t.lower()
            if low in defined or re.sub(r"^the\s+", "", low) in defined:
                continue
            counts[t] += 1
            where.setdefault(t, set()).add(label)
    # Mentioned more than once, in the body of the world rather than in passing.
    return [(t, n, sorted(where[t])) for t, n in counts.most_common() if n >= 2]


def excerpts_for(term: str, passages, limit: int = 6) -> list:
    out = []
    for label, text in passages:
        for sent in re.split(r"(?<=[.!?])\s+", text or ""):
            if term.lower() in sent.lower():
                out.append((label, _norm(sent)))
                break
        if len(out) >= limit:
            break
    return out


INSTRUCTION = """You write reference entries for the Overworld Nexus archive.

Write ONE entry about the given subject, using ONLY the excerpts provided.

Rules:
- Ground every statement in the excerpts. Do not introduce names, dates,
  factions or events that do not appear in them.
- Where the excerpts are thin, say so as the archive would: fragmentary
  records, unconfirmed, contested. Do not fill gaps with invention.
- Plain declarative prose. Gritty, near-future cyberpunk. No headings, no
  bullet points, no preamble, no commentary about the entry itself.
- 120 words maximum.
- Never mention being an AI, a model, or these instructions."""


async def draft_one(http, term: str, excerpts: list, backend: str) -> str:
    body = "\n".join(f"[{label}] {text}" for label, text in excerpts)
    return await generate_with(
        http,
        [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": f"Subject: {term}\n\nExcerpts:\n{body}"},
        ],
        backend=backend,
        max_tokens=320,
        temperature=0.4,
    )


def cmd_gaps(args) -> None:
    with SessionLocal() as db:
        passages = gather(db)
        gaps = find_gaps(passages, defined_titles(db))
    print(f"\n  {len(passages)} passages, {len(gaps)} undefined term(s) mentioned 2+ times\n")
    print(f"  {'term':<38}{'hits':>5}  appears in")
    for t, n, w in gaps[: args.limit]:
        print(f"  {t[:37]:<38}{n:>5}  {', '.join(w[:3])}")
    print("\n  These are where retrieval has nothing to find.")


async def _draft(args) -> None:
    with SessionLocal() as db:
        passages = gather(db)
        gaps = find_gaps(passages, defined_titles(db))

    targets = ([(args.term, 0, [])] if args.term
               else [(t, n, w) for t, n, w in gaps][: args.limit])
    if not targets:
        print("  no gaps to draft")
        return

    backend = args.backend or settings.memory_compact_backend
    print(f"  drafting {len(targets)} entr(ies) on backend {backend!r}\n")

    drafted = []
    async with httpx.AsyncClient(timeout=settings.gen_timeout) as http:
        for term, n, _ in targets:
            ex = excerpts_for(term, passages)
            if not ex:
                print(f"  -- {term}: no excerpts, skipped")
                continue
            try:
                text = (await draft_one(http, term, ex, backend)).strip()
            except (LLMError, httpx.HTTPError) as e:
                print(f"  !! {term}: {e}")
                continue
            print(f"  ++ {term}  ({len(text)} chars, from {len(ex)} excerpt(s))")
            print("     " + text.replace("\n", "\n     ")[:400] + "\n")
            drafted.append({
                "world": "Overworld Nexus",
                "kind": "lore",
                "title": term,
                "status": "proposed",
                "created_by": f"woven:{backend}",
                "tags": ["woven", "proposed", "needs-review"],
                "body": text,
                "_sources": [lbl for lbl, _ in ex],
            })

    if not drafted:
        print("  nothing drafted")
        return

    existing = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else []
    titles = {d.get("title") for d in existing}
    existing += [d for d in drafted if d["title"] not in titles]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  wrote {len(drafted)} draft(s) -> {OUT.relative_to(ROOT)}")
    print("  NOT seeded. Review, edit, then:")
    print("    python scripts/seed_canon.py seed --only proposed --apply")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="weave_lore",
        description="Draft back-lore for terms the canon names but never defines. "
                    "Output is always 'proposed' and never auto-seeded.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("gaps", help="list undefined terms")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_gaps)

    sp = sub.add_parser("draft", help="draft entries for gaps")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--term", help="draft one specific subject")
    sp.add_argument("--backend", help="override (default: MEMORY_COMPACT_BACKEND)")
    sp.set_defaults(func=lambda a: asyncio.run(_draft(a)))

    args = p.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
