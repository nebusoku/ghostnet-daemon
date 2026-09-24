#!/usr/bin/env python3
"""
Turn an authored Markdown lore document into canon JSON.

The weaver exists because canon had gaps. It filled them badly: asked to write
120 words about the Spire District from the single source clause
"(luxury, power, corporate towers)", a 7B model invented elevated tracks,
holographic billboards and a wealth-gap editorial. That is not a prompt
failure, it is an evidence failure -- one clause cannot ground a paragraph.

This is the other fix: author the evidence, then import it. Hand-written lore
outranks anything woven, so imported documents default to status=active.

    python scripts/import_lore.py lore/overworld.md            # parse + report
    python scripts/import_lore.py lore/overworld.md -o canon/lore.json
    python scripts/seed_canon.py seed --only lore --apply      # then seed

FORMAT
------
    # Districts                     <- section; contributes a tag + defaults
    kind: location

    ## Spire District               <- one retrievable document per H2
    tags: spire, corporate

    Body prose. Blank-line separated paragraphs are preserved.

    ### Sub-heading                 <- stays inside the body verbatim

Metadata is `key: value` lines placed directly under a heading with no blank
line between. Recognised keys: kind, status, tags, world, created_by. H1
metadata applies to every H2 beneath it until the next H1; H2 metadata wins.

Anything naming real-world influences must carry `status: gm-only` -- seeding
withholds those from retrieval, so the daemon cannot cite them in-world.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

META_KEYS = {"kind", "status", "tags", "world", "created_by"}
KNOWN_KINDS = {"setting", "faction", "rule", "npc", "lore", "location",
               "item", "event", "org", "tech"}
KNOWN_STATUS = {"active", "proposed", "gm-only", "retired"}

# One document is one vector. Too short and there is nothing to match on; too
# long and the embedding averages several subjects into mush and retrieves for
# none of them. Authored canon in this repo sits at 60-160 words.
MIN_WORDS, MAX_WORDS = 40, 450

DEFAULTS = {"world": "Overworld Nexus", "kind": "lore",
            "status": "active", "created_by": "authored"}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_meta(lines, i):
    """Consume `key: value` lines starting at i. Returns (meta, next_index)."""
    meta = {}
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            break
        m = re.match(r"^([A-Za-z_]+)\s*:\s*(.+)$", line)
        if not m or m.group(1).lower() not in META_KEYS:
            break
        key, value = m.group(1).lower(), m.group(2).strip()
        meta[key] = ([t.strip() for t in re.split(r"[,;]", value) if t.strip()]
                     if key == "tags" else value)
        i += 1
    return meta, i


def strip_comments(text):
    """
    Drop `<!-- ... -->` blocks, keeping line numbers intact.

    Authoring notes live in comments, including the multi-line header every
    lore file starts with. Without this every one of those lines reports as
    stray prose and buries the warnings that matter.
    """
    return re.sub(r"<!--.*?-->",
                  lambda m: "\n" * m.group(0).count("\n"),
                  text, flags=re.S)


def parse(text):
    text = strip_comments(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = text.split("\n")
    docs, warnings = [], []
    state = {"section_meta": {}, "section_tag": None, "current": None,
             "body": []}

    def close():
        current = state["current"]
        if current is None:
            return
        prose = "\n".join(state["body"]).strip()
        prose = re.sub(r"\n{3,}", "\n\n", prose)
        doc = dict(DEFAULTS)
        doc.update(state["section_meta"])
        doc.update(current["meta"])
        tags = list(doc.get("tags") or [])
        for extra in (state["section_tag"], slug(current["title"])):
            if extra and extra not in tags:
                tags.append(extra)
        doc["tags"] = tags
        doc["title"] = current["title"]
        doc["body"] = prose
        doc["_line"] = current["line"]
        docs.append(doc)

    i = 0
    while i < len(lines):
        line = lines[i]
        h1 = re.match(r"^#\s+(.*\S)\s*$", line)
        h2 = re.match(r"^##\s+(.*\S)\s*$", line)

        if h2:
            close()
            meta, i = parse_meta(lines, i + 1)
            state["current"] = {"title": h2.group(1).strip(), "meta": meta,
                                "line": i}
            state["body"] = []
            continue
        if h1:
            close()
            state["current"], state["body"] = None, []
            title = h1.group(1).strip()
            state["section_tag"] = slug(title)
            state["section_meta"], i = parse_meta(lines, i + 1)
            continue

        if state["current"] is not None:
            state["body"].append(line)
        elif line.strip():
            warnings.append("line %d: text outside any '## ' heading, "
                            "dropped: %r" % (i + 1, line.strip()[:60]))
        i += 1

    close()

    seen = {}
    for d in docs:
        n = len(d["body"].split())
        where = "%r (line %d)" % (d["title"], d["_line"])
        if d["kind"] not in KNOWN_KINDS:
            warnings.append("%s: unknown kind %r (known: %s)"
                            % (where, d["kind"], ", ".join(sorted(KNOWN_KINDS))))
        if d["status"] not in KNOWN_STATUS:
            warnings.append("%s: unknown status %r" % (where, d["status"]))
        if n < MIN_WORDS:
            warnings.append("%s: only %d words -- too thin to embed usefully "
                            "(min %d)" % (where, n, MIN_WORDS))
        if n > MAX_WORDS:
            warnings.append("%s: %d words -- one vector will average several "
                            "subjects; split it (max %d)"
                            % (where, n, MAX_WORDS))
        key = d["title"].lower()
        if key in seen:
            warnings.append("%s: duplicate title, also at line %d"
                            % (where, seen[key]))
        seen[key] = d["_line"]

    return docs, warnings


def main():
    p = argparse.ArgumentParser(
        prog="import_lore",
        description="Parse an authored Markdown lore doc into canon JSON.")
    p.add_argument("markdown", type=Path)
    p.add_argument("-o", "--out", type=Path,
                   help="write canon JSON here (default: report only)")
    p.add_argument("--force", action="store_true",
                   help="write even if there are warnings")
    args = p.parse_args()

    if not args.markdown.is_file():
        sys.exit("no such file: %s" % args.markdown)

    docs, warnings = parse(args.markdown.read_text(encoding="utf-8"))
    if not docs:
        sys.exit("no '## ' headings found -- nothing to import")

    print("\n  %d document(s) from %s\n" % (len(docs), args.markdown))
    for d in docs:
        n = len(d["body"].split())
        flag = "  GM" if d["status"] == "gm-only" else ""
        note = ""
        if "CONFLICT" in d["body"]:
            note = "  [CONFLICT]"
        elif "STUB" in d["body"]:
            note = "  [STUB]"
        print("    %-10s %-9s %4dw  %s%s%s"
              % (d["kind"], d["status"], n, d["title"][:44], flag, note))

    held = [d for d in docs if d["status"] == "gm-only"]
    if held:
        print("\n  %d document(s) are GM-ONLY and will be withheld from "
              "retrieval." % len(held))

    if warnings:
        print("\n  %d warning(s):" % len(warnings))
        for w in warnings:
            print("    ! %s" % w)

    chars = sum(len(d["body"]) for d in docs)
    print("\n  total body: %s chars (~%.1f min to embed)"
          % ("{:,}".format(chars), len(docs) * 2.5 / 60))

    if not args.out:
        print("\n  report only -- pass -o canon/lore.json to write")
        return 0
    if warnings and not args.force:
        print("\n  NOT written: fix the warnings above, or pass --force")
        return 1

    payload = [{k: v for k, v in d.items() if not k.startswith("_")}
               for d in docs]
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False)
                        + "\n", encoding="utf-8")
    print("\n  wrote %d document(s) -> %s" % (len(payload), args.out))
    print("  next: python scripts/seed_canon.py seed --only %s --apply"
          % args.out.stem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
