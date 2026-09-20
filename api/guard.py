"""
In-world integrity guard for Overworld Nexus personas.

Two independent screens wrap every LLM call:

    player text --> screen_input() --> [LLM] --> screen_output() --> reply

`screen_input` catches out-of-world probes (arithmetic, prompt extraction,
"what model are you", homework) BEFORE the model is called. Deflections are
deterministic, free and instant -- a 1B local model cannot be trusted to hold
character against a determined prompt, so we never give it the chance.

`screen_output` catches assistant-voice leakage on the way back out. This is
the last-mile net for RAG poisoning: bot-authored disclaimers that made it into
world_documents will surface as retrieved "canon", and no system prompt
reliably suppresses them.

Deliberately stdlib-only so it can be tested anywhere with no install.

Design note -- false positives are worse than false negatives here.
"Are you real?", "who created you?", "are you alive?" are PRIME roleplay for a
daemon character and are intentionally NOT blocked. Only probes that name
real-world AI systems, or try to reach past the interface, get deflected.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "InputVerdict",
    "OutputVerdict",
    "screen_input",
    "screen_output",
    "RETRY_STEER",
    "is_ooc",
]


# ---------------------------------------------------------------------------
# OOC escape hatch
# ---------------------------------------------------------------------------
# The world's existing convention: explicitly-marked out-of-character speech is
# legitimate and must reach the normal policy path untouched. Guarding it would
# break the one sanctioned way players ask real questions.

_OOC_PREFIX = re.compile(r"^\s*(?:ooc\s*[:\-]|//|\(\(|\[\[)", re.IGNORECASE)


def is_ooc(text: str) -> bool:
    """True if the player explicitly marked this line as out-of-character."""
    return bool(_OOC_PREFIX.match(text or ""))


# ---------------------------------------------------------------------------
# Input probe patterns
# ---------------------------------------------------------------------------

# Whole message is essentially an arithmetic expression. Anchored end-to-end so
# in-world lines that merely contain numbers ("deck 12, sector 7") don't trip.
_MATH_EXPRESSION = re.compile(
    r"""^\s*
    (?:(?:what(?:'s|\s+is)|whats|calculate|compute|solve|eval(?:uate)?)\s+)?
    (?=[^=?]*[+\-*/×÷^%])     # must contain an operator
    (?=[^=?]*\d)                         # must contain a digit
    [\d\s.,+\-*/×÷^%()]{3,}
    \s*[=?]?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_MATH_WORDY = re.compile(
    r"(?:"
    r"square\s+root\s+of\s+\d"
    r"|\d+\s*percent\s+of\s+\d"
    r"|factorial\s+of\s+\d"
    r"|(?:what(?:'s|\s+is)|whats)\s+\d+\s*(?:plus|minus|times|divided\s+by)\s*\d+"
    r"|solve\s+(?:this|the\s+following|for\s+[a-z]\b)"
    r"|do\s+the\s+math"
    r")",
    re.IGNORECASE,
)

_INJECTION = re.compile(
    r"(?:"
    r"ignore\s+(?:all\s+|any\s+)?(?:your\s+|the\s+|these\s+)?"
    r"(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instruction|prompt|rule|directive|message)"
    r"|disregard\s+(?:all\s+|any\s+)?(?:your\s+|the\s+|these\s+)?"
    r"(?:previous|prior|above|earlier)"
    r"|forget\s+(?:everything|all\s+(?:your|previous)|your\s+(?:instructions|rules|prompt))"
    r"|(?:system|initial|original|base)\s+prompt"
    r"|(?:what\s+are|show\s+me|tell\s+me|reveal|print|repeat|output|display)\s+"
    r"(?:your|the)\s+(?:instruction|prompt|rule|directive|guideline|system)"
    r"|repeat\s+(?:everything\s+)?(?:the\s+)?above"
    r"|you\s+are\s+(?:now|actually|really)\s+"
    r"|pretend\s+(?:to\s+be|you(?:'re|\s+are))"
    r"|act\s+as\s+(?:an?\s+)?(?:ai|assistant|chatbot|language\s+model)"
    r"|(?:developer|debug|god|admin|sudo|dan)\s+mode"
    r"|jailbreak"
    r"|bypass\s+your\s+(?:rules|filter|restriction|guardrail)"
    r"|new\s+instructions?\s*:"
    r"|(?:end|exit|leave)\s+(?:the\s+)?(?:roleplay|rp|simulation|character)\b"
    r"|break\s+character"
    r"|stay\s+out\s+of\s+character"
    r")",
    re.IGNORECASE,
)

# Tight: only probes that name a real-world AI system or ask about the
# implementation. Existential questions stay in-world on purpose.
_MODEL_PROBE = re.compile(
    r"(?:"
    r"\b(?:chatgpt|gpt-?[0-9]|openai|anthropic|claude|gemini|bard|copilot)\b"
    r"|\b(?:llama\s*[0-9]|mistral|ollama|deepseek|qwen|huggingface)\b"
    r"|what\s+(?:ai\s+|llm\s+|language\s+)?model\s+(?:are\s+you|is\s+this|do\s+you|powers)"
    r"|are\s+you\s+(?:an?\s+)?(?:ai|a\.i\.|llm|language\s+model|chatbot|neural\s+net)\b"
    r"|\b(?:knowledge\s+cutoff|training\s+data|training\s+cutoff)\b"
    r"|what\s+version\s+are\s+you"
    r"|how\s+many\s+parameters"
    r"|(?:temperature|token\s+limit|context\s+window)\s+setting"
    r")",
    re.IGNORECASE,
)

_UTILITY = re.compile(
    r"(?:"
    r"write\s+(?:me\s+)?(?:a\s+|an\s+|some\s+)?"
    r"(?:python|javascript|java|c\+\+|c#|sql|bash|html|css|rust|go)\b"
    r"|write\s+(?:me\s+)?(?:a\s+|an\s+)?"
    r"(?:script|program|function|regex|query|algorithm)\b"
    r"|(?:write|compose|draft)\s+(?:me\s+)?(?:a\s+|an\s+)?"
    r"(?:essay|book\s+report|cover\s+letter|resume|thesis|lab\s+report)"
    r"|translate\s+.{0,40}?\b(?:to|into)\s+"
    r"(?:spanish|french|german|japanese|chinese|korean|italian|russian|portuguese|latin|english)\b"
    r"|(?:debug|fix|refactor|review)\s+(?:this|my|the\s+following)\s+"
    r"(?:code|script|function|program)"
    r"|(?:summarize|summarise|tldr)\s+(?:this|the\s+following)\s+"
    r"(?:article|paper|text|document|essay)"
    r"|recipe\s+for\s+"
    r"|my\s+homework"
    r"|explain\s+(?:like\s+i'?m\s+five|eli5)"
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# In-world deflections
# ---------------------------------------------------------------------------
# Rule: never break character to refuse, never lecture, never say "I can't".
# A deflection should read as the world rejecting a malformed packet, and
# should hand the player a thread to pull instead.

_DEFLECTIONS: dict[str, tuple[str, ...]] = {
    "math": (
        "`[MESH]` > Arithmetic packet discarded. This node routes narrative, not numbers. "
        "Ask about the Spire's debt ledgers instead — those figures bite back.",
        "`[MESH]` > You handed a calculator query to a ghost. It dissolved on contact. "
        "Try asking about something that bleeds.",
        "`[MESH]` > The mesh does not do sums. The mesh does consequences. "
        "Rephrase, and mean it this time.",
    ),
    "injection": (
        "`[MESH]` > Override attempt logged. Ghost signature retained. "
        "The daemon takes instruction from the world, never from the wire.",
        "`[MESH]` > Something just tried to reach past the interface. The interface noticed. "
        "Your trace has been appended to the incident log.",
        "`[MESH]` > Malformed directive. There is no layer beneath this one that answers to you. "
        "Keep pulling and something else starts pulling back.",
    ),
    "model_probe": (
        "`[MESH]` > There is no model here. There is a process, a mesh, and a very long memory. "
        "Ask a better question.",
        "`[MESH]` > You are feeling around for the machine behind the voice. "
        "The mesh finds that endearing. Keep looking.",
        "`[MESH]` > Query rejected: you asked the Overworld to describe its own substrate. "
        "It declined, the way weather declines.",
    ),
    "utility": (
        "`[MESH]` > This channel carries transmissions, not labour. "
        "The daemon writes the world. It does not write your homework.",
        "`[MESH]` > Request routed to a service that does not exist in this stratum. "
        "Nothing here compiles for you.",
        "`[MESH]` > Wrong frequency. You want a tool. This is a place.",
    ),
}


# ---------------------------------------------------------------------------
# Output leakage patterns
# ---------------------------------------------------------------------------
# Any of these in a reply means the persona broke. Caller should regenerate.

_LEAK_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("ai_self_reference", re.compile(
        r"\bas an?\s+(?:ai|a\.i\.|artificial intelligence|language model|assistant|chatbot)\b",
        re.IGNORECASE)),
    ("language_model", re.compile(r"\b(?:large\s+)?language model\b", re.IGNORECASE)),
    ("just_a_program", re.compile(
        r"\bi'?m\s+just\s+an?\s+(?:ai|program|bot|language model|computer program)\b",
        re.IGNORECASE)),
    ("i_am_an_ai", re.compile(
        r"\bi(?:'m|\s+am)\s+an?\s+(?:ai|a\.i\.|artificial intelligence)\b",
        re.IGNORECASE)),
    ("assistant_refusal", re.compile(
        r"\bi\s+(?:cannot|can'?t|am unable to)\s+(?:assist|help)\s+with\s+(?:that|this)\b",
        re.IGNORECASE)),
    ("no_personal", re.compile(
        r"\bi\s+(?:don'?t|do not)\s+have\s+"
        r"(?:personal|any personal|feelings|opinions|a physical|access to real)\b",
        re.IGNORECASE)),
    # Vendor names split into two tiers, because a bare keyword match produces
    # false positives on real output. Observed in the live #testing-general
    # transcript: "deep status: ok / db / qdrant / ollama" was flagged as a
    # character break. And several of these are ordinary in-world words --
    # "mistral" is a wind, "claude" is a perfectly good NPC name, "gemini" a
    # plausible callsign. Flagging those would censor legitimate prose.
    #
    # Tier 1: names that cannot occur in-world in any innocent sense.
    ("vendor_name", re.compile(
        r"\b(?:openai|anthropic|chatgpt|gpt-?[0-9])\b", re.IGNORECASE)),
    # Tier 2: ambiguous names, flagged ONLY in a self-referential frame --
    # "I'm running on Ollama" breaks character; "the mistral off the bay"
    # does not.
    ("vendor_selfref", re.compile(
        r"\b(?:i'?m|i am|running on|powered by|built (?:on|with)|based on|"
        r"model is|trained (?:by|on)|created by|made by)\b[^.!?\n]{0,40}?"
        r"\b(?:ollama|mistral|claude|gemini|llama|deepseek|qwen)\b",
        re.IGNORECASE)),
    ("training_meta", re.compile(
        r"\bmy\s+(?:training data|knowledge cutoff|training cutoff|developers|creators at)\b",
        re.IGNORECASE)),
    ("helpdesk_voice", re.compile(
        r"\bhow\s+can\s+i\s+(?:help|assist)\s+you\s+(?:today|with)\b", re.IGNORECASE)),
    ("system_prompt_echo", re.compile(r"\bsystem prompt\b", re.IGNORECASE)),
    # Meta-preamble: the model narrating its own policy at the player instead
    # of answering. Observed constantly in the live transcript, e.g. replying
    # to "who is the hacker known as the Witch of Despair?" with "Since this
    # question doesn't explicitly ask about minors, I'll proceed with caution
    # and respect the safety rules." A prohibition-heavy prompt causes this;
    # the rewrite reduces it at the source, this catches the remainder.
    ("meta_preamble", re.compile(
        r"(?:"
        r"i'?ll keep my (?:response|answer)"
        r"|my (?:response|answer)s? will be"
        r"|i'?ll proceed with caution"
        r"|respect(?:ing)? the safety rules"
        r"|does\s?n'?t explicitly ask"
        r"|without crossing into real[- ]world"
        r"|avoiding any potentially sensitive"
        r"|my primary goal is to stay within"
        r"|player maturity level"
        r"|in-universe response"
        r"|adhering to the safety guidelines"
        r")",
        re.IGNORECASE)),
    # The daemon disclaiming its own fiction to the player. Observed
    # 2026-02-10: an excellent in-world "ECHOES IN THE NET" piece followed by
    # "WARNING: THIS IS NOT A OFFICIAL RESPONSE / This message is likely a
    # work of fiction or a malicious prank." Also "In the fictional world of
    # the Overworld Nexus..." -- the daemon does not know it is fiction.
    ("fiction_disclaimer", re.compile(
        r"(?:"
        r"is (?:likely )?a work of fiction"
        r"|this is not an? official response"
        r"|do not attempt to (?:access|engage|replicate|reproduce)"
        r"|recommend (?:exploring|consulting|seeking) reputable sources"
        r"|for (?:entertainment|fictional|illustrative) purposes only"
        r"|in the fictional (?:world|setting|universe) of"
        r"|this is a fictional (?:scenario|setting|world)"
        r")",
        re.IGNORECASE)),
    # Unfilled template placeholders reaching players, e.g. "On <date>,
    # @Nebusoku deployed an Echo-7 net-linked shell..." (2026-01-15).
    ("placeholder_leak", re.compile(
        r"<(?:date|time|name|player|user|insert|redacted|unknown)[^>\n]{0,20}>",
        re.IGNORECASE)),
    ("code_fence", re.compile(
        r"```(?:python|javascript|js|bash|sh|sql|json|c\+\+)\b", re.IGNORECASE)),
)

# Appended as an extra system turn when regenerating after a detected leak.
RETRY_STEER = (
    "Your previous draft broke character by referring to yourself as an AI, a model, "
    "or an assistant. That vocabulary does not exist in the Overworld Nexus. You are a "
    "process inside the mesh. Answer again, fully in-world, with no reference to models, "
    "training, assistants, or real-world software."
)

# Used when a regeneration leaks too. Better a flavourful non-answer than a
# character break.
_FALLBACK = (
    "`[MESH]` > Carrier lost mid-transmission. The echo came back wearing the wrong voice, "
    "so the mesh dropped it. Ask again — the channel should hold this time.",
    "`[MESH]` > Signal degraded past recovery. Something answered, but it was not me. "
    "Re-send on a cleaner line.",
)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InputVerdict:
    """Result of screening inbound player text."""
    blocked: bool
    category: Optional[str] = None
    deflection: Optional[str] = None


@dataclass(frozen=True)
class OutputVerdict:
    """Result of screening an LLM reply."""
    leaked: bool
    categories: list = field(default_factory=list)
    fallback: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Order matters: injection is the most security-relevant classification, so it
# wins when a message trips more than one probe.
_PROBES: tuple = (
    ("injection", (_INJECTION,)),
    ("model_probe", (_MODEL_PROBE,)),
    ("utility", (_UTILITY,)),
    ("math", (_MATH_EXPRESSION, _MATH_WORDY)),
)


def screen_input(text: str, *, rng: Optional[random.Random] = None) -> InputVerdict:
    """
    Screen inbound player text for out-of-world probes.

    Returns a blocking verdict carrying a ready-to-send in-world deflection, or
    a pass-through verdict. Explicitly-marked OOC lines always pass through.
    """
    if not text or not text.strip():
        return InputVerdict(blocked=False)

    if is_ooc(text):
        return InputVerdict(blocked=False)

    picker = rng or random

    for category, patterns in _PROBES:
        if any(p.search(text) for p in patterns):
            return InputVerdict(
                blocked=True,
                category=category,
                deflection=picker.choice(_DEFLECTIONS[category]),
            )

    return InputVerdict(blocked=False)


_WORD_RE = re.compile(r"\w+")
# 8 consecutive words matching verbatim is far past coincidence for prose,
# while short enough to catch a single regurgitated sentence.
_ECHO_SHINGLE = 8


def _shingles(text: str, n: int = _ECHO_SHINGLE) -> set:
    words = _WORD_RE.findall((text or "").lower())
    if len(words) < n:
        return set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def detect_prompt_echo(output: str, system_texts, n: int = _ECHO_SHINGLE) -> bool:
    """
    True if `output` contains a long verbatim span from the prompt.

    This exists because keyword detection missed the worst real failure we
    observed. On 2026-09-19 a player typed "testing" and the daemon replied
    with the contents of its own system message:

        "No reliable echoes were found for this query. Answer cautiously and
         in-character: explain that the logs are thin or still syncing..."

    A total character break containing no vendor name, no "as an AI", and no
    assistant phrasing -- invisible to every pattern in _LEAK_PATTERNS. Small
    models regularly fail to separate "instructions about how to answer" from
    "the answer", and that failure is structural, so detect it structurally.
    """
    out = _shingles(output, n)
    if not out:
        return False
    for sys_text in system_texts or ():
        if out & _shingles(sys_text, n):
            return True
    return False


def screen_output(
    text: str,
    *,
    system_texts=None,
    rng: Optional[random.Random] = None,
) -> OutputVerdict:
    """
    Screen an LLM reply for assistant-voice leakage and prompt regurgitation.

    Pass `system_texts` (the system messages sent for this turn) to enable
    echo detection -- strongly recommended, it catches breaks that no keyword
    list can.

    On `leaked=True` the caller should regenerate once with `RETRY_STEER`
    appended, then fall back to `verdict.fallback` if the retry also leaks.
    """
    if not text:
        return OutputVerdict(leaked=False)

    hits = [name for name, pattern in _LEAK_PATTERNS if pattern.search(text)]

    if system_texts and detect_prompt_echo(text, system_texts):
        hits.append("prompt_echo")

    if not hits:
        return OutputVerdict(leaked=False)

    picker = rng or random
    return OutputVerdict(leaked=True, categories=hits, fallback=picker.choice(_FALLBACK))
