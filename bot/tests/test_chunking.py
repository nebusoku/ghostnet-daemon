"""
Tests for Discord message chunking.

Stdlib only. From the repo root:

    python -m unittest discover -s bot/tests -t bot -v

These are invariant tests rather than exact-output tests: what matters is that
no chunk can ever be rejected by Discord, no text is lost, no code fence is
left unbalanced, and paragraph structure survives.
"""

import os
import re
import sys
import unittest

# bot/ uses flat imports (from config import ...), so put it on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chunking import DISCORD_SAFE_LEN, split_for_discord  # noqa: E402


LIMIT = DISCORD_SAFE_LEN


def _norm(s: str) -> str:
    """Compare content ignoring whitespace and fence markers we re-add."""
    return re.sub(r"\s+", "", re.sub(r"```[a-z]*", "", s))


def _sentence(n: int) -> str:
    return f"The alley breathes steam and the drones drift past number {n}."


def _paragraph(n: int) -> str:
    return " ".join(_sentence(n * 10 + i) for i in range(6))


CASES = {
    "short": "The Spire does not answer questions. It files them.",
    "exactly at limit": "x" * LIMIT,
    "one over limit": "y" * (LIMIT + 1),
    "long prose": "\n\n".join(_paragraph(i) for i in range(12)),
    "no sentence breaks": "z" * 5000,
    "one giant word": "w" * 4000,
    "url unbroken": "see http://example.com/" + "a" * 2500,
    "paragraphs": "First beat.\n\nSecond beat.\n\nThird beat.",
    "code small": "Here:\n```python\nprint(1)\n```\ndone",
    "code huge": (
        "Intro.\n\n```python\n"
        + "\n".join(f"line_{i} = {i} * 2" for i in range(200))
        + "\n```\n\nOutro."
    ),
    "code long line": "```\n" + "q" * 3000 + "\n```",
    "unterminated fence": "Start.\n```python\n" + "print(1)\n" * 400,
    "mixed": _paragraph(1) + "\n\n```js\nconst a=1;\n```\n\n" + _paragraph(2),
    "many newlines": "A.\n\n\n\n\nB.\n\n\n\nC.",
    "abbreviations": "Mr. Vance met Dr. Sato on St. Verge. " + "They spoke. " * 300,
}


class TestInvariants(unittest.TestCase):
    """Properties that must hold for every input."""

    def test_never_exceeds_limit(self):
        # A chunk over the cap is rejected by Discord and the reply is lost.
        for name, text in CASES.items():
            with self.subTest(case=name):
                for chunk in split_for_discord(text, LIMIT):
                    self.assertLessEqual(len(chunk), LIMIT, f"{name}: chunk too long")

    def test_no_empty_chunks(self):
        for name, text in CASES.items():
            with self.subTest(case=name):
                for chunk in split_for_discord(text, LIMIT):
                    self.assertTrue(chunk.strip(), f"{name}: emitted empty chunk")

    def test_content_preserved(self):
        for name, text in CASES.items():
            with self.subTest(case=name):
                joined = "".join(split_for_discord(text, LIMIT))
                self.assertEqual(_norm(joined), _norm(text), f"{name}: content changed")

    def test_fences_balanced_in_every_chunk(self):
        # An odd number of fences in one message leaks backticks into the next.
        for name, text in CASES.items():
            with self.subTest(case=name):
                for chunk in split_for_discord(text, LIMIT):
                    self.assertEqual(
                        chunk.count("```") % 2, 0, f"{name}: unbalanced fence"
                    )

    def test_order_preserved(self):
        text = "\n\n".join(f"Para {i} here." for i in range(40))
        chunks = split_for_discord(text, 200)
        found = [int(m) for m in re.findall(r"Para (\d+)", " ".join(chunks))]
        self.assertEqual(found, sorted(found))


class TestEmptyInput(unittest.TestCase):
    def test_empty_returns_nothing(self):
        for text in ("", "   ", "\n", None):
            self.assertEqual(split_for_discord(text, LIMIT), [])


class TestParagraphPreservation(unittest.TestCase):
    """
    The old implementation joined sentences with a single space, flattening
    every scene break. Paragraph structure is the pacing -- it must survive.
    """

    def test_paragraph_breaks_kept_within_a_chunk(self):
        chunks = split_for_discord("First beat.\n\nSecond beat.\n\nThird beat.", LIMIT)
        self.assertEqual(len(chunks), 1)
        self.assertIn("\n\n", chunks[0])

    def test_paragraphs_not_collapsed_to_spaces(self):
        text = "\n\n".join(_paragraph(i) for i in range(3))
        self.assertIn("\n\n", "".join(split_for_discord(text, LIMIT)))


class TestWordBoundaries(unittest.TestCase):
    def test_hard_split_prefers_spaces(self):
        # Long run of real words: splits should land on spaces, not mid-word.
        text = " ".join("word" for _ in range(2000))
        for chunk in split_for_discord(text, 500):
            self.assertFalse(chunk.startswith("ord"), "cut mid-word")
            self.assertFalse(chunk.endswith("wor"), "cut mid-word")

    def test_unbreakable_run_still_fits(self):
        # No spaces to break on: must still respect the cap.
        for chunk in split_for_discord("q" * 4000, 500):
            self.assertLessEqual(len(chunk), 500)


class TestCodeBlocks(unittest.TestCase):
    def test_small_code_block_stays_intact(self):
        text = "Here:\n```python\nprint(1)\n```\ndone"
        chunks = split_for_discord(text, LIMIT)
        self.assertEqual(len(chunks), 1)

    def test_large_code_block_refenced_per_chunk(self):
        text = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(500)) + "\n```"
        chunks = split_for_discord(text, 800)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("```"), "piece not re-opened")
            self.assertTrue(chunk.rstrip().endswith("```"), "piece not closed")

    def test_unterminated_fence_does_not_double_close(self):
        # Regression: .strip() removes the trailing newline, gluing the
        # synthesized closing fence onto the last code line.
        text = "Start.\n```python\n" + "print(1)\n" * 400
        for chunk in split_for_discord(text, LIMIT):
            self.assertNotIn("```\n```", chunk)
            self.assertEqual(chunk.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
