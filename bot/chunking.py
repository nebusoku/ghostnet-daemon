"""
Split daemon replies into Discord-safe chunks.

Discord hard-caps a message at 2000 characters. This matters far more since
generation moved to a hosted backend: the old 1B model produced short replies
that rarely hit the cap, while MAX_OUTPUT_TOKENS=512 is roughly 2000+
characters, so most replies now need splitting.

Priorities, in order:

  1. Never exceed the limit (a rejected message loses the whole reply).
  2. Never split inside a code fence -- half a fence renders as garbage and
     the closing half leaks backticks into the next message.
  3. Preserve paragraph breaks. The previous implementation joined sentences
     with a single space, which flattened every scene break in atmospheric
     prose into a wall of text. Paragraph structure IS the pacing.
  4. Prefer sentence boundaries, then word boundaries. Never cut mid-word.

Pure stdlib and free of discord.py imports so it can be unit-tested directly.
"""

from __future__ import annotations

import re
from typing import List

__all__ = ["split_for_discord", "DISCORD_MAX_MESSAGE_LEN", "DISCORD_SAFE_LEN"]

DISCORD_MAX_MESSAGE_LEN = 2000
# Headroom for trailing whitespace and wide/combining unicode.
DISCORD_SAFE_LEN = 1900

# Sentence end: ., ! or ? followed by whitespace. The negative lookbehind keeps
# common abbreviations and initials from being treated as sentence ends.
_SENTENCE_RE = re.compile(
    r"(?<!\b[A-Z])(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bSt)(?<=[.!?])\s+"
)
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_FENCE_RE = re.compile(r"```")


def _hard_split(text: str, limit: int) -> List[str]:
    """Last resort for an unbroken run longer than the limit: split on spaces."""
    out: List[str] = []
    remaining = text.strip()

    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind(" ")
        # No space to break on (a URL, a long token): cut at the limit.
        if cut <= limit // 2:
            cut = limit
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        out.append(remaining)
    return out


def _split_sentences(block: str, limit: int) -> List[str]:
    """Pack one paragraph's sentences into <=limit pieces."""
    if len(block) <= limit:
        return [block]

    pieces: List[str] = []
    current = ""

    for sentence in _SENTENCE_RE.split(block):
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_split(sentence, limit))
            continue

        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= limit:
            current = f"{current} {sentence}"
        else:
            pieces.append(current)
            current = sentence

    if current:
        pieces.append(current)
    return pieces


def _split_code_block(block: str, limit: int) -> List[str]:
    """
    Split a fenced code block across messages, re-fencing each piece so every
    message renders as valid code on its own.
    """
    # Strip the closing fence from the block as a STRING, not as a line.
    # An unterminated fence that reached us via _blocks() has had a closing
    # fence synthesized onto it, and split_for_discord() has already stripped
    # the trailing newline -- so the fence can be glued to the last code line
    # ("print(1)```") rather than sitting on its own. A line-wise check misses
    # that and emits a doubled fence.
    block = block.rstrip()
    if block.endswith("```"):
        block = block[:-3].rstrip("\n")

    lines = block.split("\n")
    opener = lines[0] if lines and lines[0].startswith("```") else "```"
    body = lines[1:] if len(lines) > 1 else []

    # Each emitted piece costs the opener, a newline, and a closing fence.
    overhead = len(opener) + 5
    budget = max(64, limit - overhead)

    out: List[str] = []
    current: List[str] = []
    size = 0

    for line in body:
        # A single line longer than the budget has to be broken on its own.
        if len(line) > budget:
            if current:
                out.append(f"{opener}\n" + "\n".join(current) + "\n```")
                current, size = [], 0
            for piece in _hard_split(line, budget):
                out.append(f"{opener}\n{piece}\n```")
            continue

        if size + len(line) + 1 > budget and current:
            out.append(f"{opener}\n" + "\n".join(current) + "\n```")
            current, size = [], 0

        current.append(line)
        size += len(line) + 1

    if current:
        out.append(f"{opener}\n" + "\n".join(current) + "\n```")
    return out or [block[:limit]]


def _blocks(text: str) -> List[tuple]:
    """
    Break text into ('code'|'prose', content) segments on fence boundaries.

    An unterminated fence is treated as running to the end of the text, which
    is what Discord itself does when rendering.
    """
    if not _FENCE_RE.search(text):
        return [("prose", text)]

    out: List[tuple] = []
    parts = text.split("```")
    # Even indices are outside fences, odd indices inside.
    for i, part in enumerate(parts):
        if not part:
            continue
        if i % 2 == 0:
            out.append(("prose", part))
        else:
            out.append(("code", f"```{part}```"))
    return out


def split_for_discord(text: str, limit: int = DISCORD_SAFE_LEN) -> List[str]:
    """
    Split `text` into ordered chunks that each fit within `limit`.

    Paragraph breaks are preserved wherever two paragraphs fit in the same
    chunk; code fences are never split across a message boundary without being
    re-opened. Returns [] for empty input.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    def add(piece: str, joiner: str) -> None:
        """Append a piece to the current chunk, starting a new one if needed."""
        nonlocal current
        if not current:
            current = piece
        elif len(current) + len(joiner) + len(piece) <= limit:
            current = f"{current}{joiner}{piece}"
        else:
            flush()
            current = piece

    for kind, block in _blocks(text):
        if kind == "code":
            # Code blocks are atomic: never merge them into surrounding prose,
            # so a fence can't end up straddling a message boundary.
            flush()
            for piece in ([block] if len(block) <= limit
                          else _split_code_block(block, limit)):
                chunks.append(piece)
            continue

        for paragraph in _PARAGRAPH_RE.split(block):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            for piece in _split_sentences(paragraph, limit):
                # "\n\n" keeps the paragraph break visible when two paragraphs
                # share a chunk -- this is the bit the old implementation lost.
                add(piece, "\n\n")

    flush()
    return [c for c in chunks if c]
