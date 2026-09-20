"""
Pluggable text-generation backend with provider failover.

Generation and embedding are deliberately decoupled, because on the GhostNet VM
they have opposite performance characteristics:

  * Embedding is a single forward pass. `nomic-embed-text` measured ~2.5s/call
    locally -- fine. Embeddings stay on Ollama (see api/rag.py).
  * Generation is autoregressive and this CPU has no AVX2/FMA, so a 7B tops out
    near 2.7 tok/s -- 95s for a 256-token reply. Unusable for live play.

So `/chat` routes through this module and can point at a hosted endpoint while
Qdrant, embeddings, Postgres and the bots stay on the VM unchanged.

Providers
---------
  ollama      native /api/chat on the VM.
  openai      any OpenAI-compatible /v1/chat/completions.
  openrouter  same wire format, separate credentials -- useful as a free-tier
              standby when the primary is down or rate-limited.

Because `openai` and `openrouter` share the OpenAI wire format, adding Groq,
Together, vLLM or LM Studio is a base-URL change, not a code change.

Failover
--------
LLM_CHAIN is an ordered, comma-separated list, e.g.

    LLM_CHAIN=openai,openrouter,ollama

Each provider is tried in order until one returns text. A provider is skipped
on transport errors, timeouts, 429s, auth failures, 5xx, and missing config --
anything suggesting "this provider cannot serve the request right now". The
last provider's failure propagates so the caller can render it in-world.

Put the local backend last: it always answers, so it makes a good floor, but
it is the slowest and weakest and should never be reached first.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from .settings import settings

__all__ = [
    "LLMError",
    "ProviderUnavailable",
    "generate",
    "generate_with",
    "describe_backend",
    "resolve_chain",
]


class LLMError(RuntimeError):
    """Backend returned something we cannot interpret, or is misconfigured."""


class ProviderUnavailable(LLMError):
    """This provider cannot serve right now; the chain should try the next."""


# Ollama sampling options.
#
# num_thread is LOAD-BEARING on the GhostNet VM -- do not remove it, and do not
# raise it to the core count. Measured on llama3.2:1b, same prompt:
#
#     num_thread = 1                     ->  2.78 tok/s
#     num_thread = 6                     ->  9.47 tok/s
#     num_thread = 8                     -> 10.91 tok/s   <-- best
#     num_thread = 12                    -> 10.07 tok/s
#     num_thread = 24 (all vCPUs)        ->  0.33 tok/s   <-- collapse
#     num_thread unset (Ollama default)  ->  0.31 tok/s   <-- collapse
#
# Anything in 6-12 is fine; 24 (and Ollama's own default on this host) is ~30x
# worse. The host is a 4-socket Xeon E5-4617 under KVM, 6 cores per socket,
# with Qdrant/Postgres/the bots competing for the same CPUs -- saturating all
# 24 vCPUs appears to induce cross-socket traffic and vCPU steal. Pinning the
# count avoids the cliff. Retune with a sweep if CPU allocation changes.
#
# The CPU exposes only AVX/SSE4.2 -- no AVX2, no FMA (Sandy Bridge-EP, 2012),
# so llama.cpp runs its AVX-only path and absolute throughput sits roughly
# 2-3x below a modern core.
OLLAMA_OPTS = {
    "temperature": 0.6,
    "repeat_penalty": 1.1,
}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    name: str
    kind: str                  # "ollama" | "openai_compatible"
    model: str
    base_url: str = ""
    api_key: str = ""
    headers: dict = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.name}:{self.model or '(unset)'}"


def _openrouter_models() -> List[str]:
    """Ordered OpenRouter model ids, falling back to the single-model setting."""
    raw = (settings.openrouter_models or "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return [settings.openrouter_model] if settings.openrouter_model else []


def _provider(name: str) -> Provider:
    name = (name or "").strip().lower()

    # "openrouter#<model>" addresses one specific model. resolve_chain()
    # expands a bare "openrouter" into one of these per configured model, so
    # an upstream 429 on one model falls through to the next using the same
    # failover loop that handles whole providers.
    if name.startswith("openrouter#"):
        model = name.split("#", 1)[1]
        p = _provider("openrouter")
        return Provider(
            name=f"openrouter[{model}]",
            kind=p.kind,
            model=model,
            base_url=p.base_url,
            api_key=p.api_key,
            headers=p.headers,
        )

    if name == "ollama":
        return Provider(
            name="ollama",
            kind="ollama",
            model=settings.chat_model,
            base_url=settings.ollama_url,
        )

    if name == "openai":
        return Provider(
            name="openai",
            kind="openai_compatible",
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )

    if name == "openrouter":
        extra = {}
        # OpenRouter uses these for attribution; harmless elsewhere.
        if settings.openrouter_referer:
            extra["HTTP-Referer"] = settings.openrouter_referer
        if settings.openrouter_title:
            extra["X-Title"] = settings.openrouter_title
        return Provider(
            name="openrouter",
            kind="openai_compatible",
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            headers=extra,
        )

    raise LLMError(
        f"Unknown provider {name!r}. Expected one of: ollama, openai, openrouter."
    )


def resolve_chain() -> List[str]:
    """
    Ordered provider names from LLM_CHAIN, falling back to LLM_BACKEND.

    A bare "openrouter" expands into one entry per configured model, so a
    chain of "openai,openrouter,ollama" with three OpenRouter models becomes
    five links. Free models are rate-limited upstream independently of each
    other, so trying the next model is usually enough to get served.
    """
    raw = (settings.llm_chain or "").strip()
    if not raw:
        raw = settings.llm_backend or "ollama"

    names: List[str] = []
    for part in raw.split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p == "openrouter":
            models = _openrouter_models()
            names.extend(f"openrouter#{m}" for m in models) if models else names.append(p)
        else:
            names.append(p)

    return names or ["ollama"]


def describe_backend() -> str:
    """Human-readable chain description, for /health and logs."""
    parts = []
    for name in resolve_chain():
        try:
            parts.append(_provider(name).describe())
        except LLMError:
            parts.append(f"{name}:(invalid)")
    return " -> ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate(
    http: httpx.AsyncClient,
    messages: List[dict],
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    """
    Generate a completion, walking LLM_CHAIN until a provider succeeds.

    Raises the final provider's error if every provider fails.
    """
    chain = resolve_chain()
    last_error: Optional[Exception] = None

    for idx, name in enumerate(chain):
        try:
            return await generate_with(
                http, messages,
                backend=name,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except (ProviderUnavailable, httpx.ReadTimeout, httpx.ConnectError,
                httpx.RemoteProtocolError) as e:
            last_error = e
            remaining = chain[idx + 1:]
            if remaining:
                print(
                    f"[llm] provider '{name}' unavailable ({type(e).__name__}: "
                    f"{str(e)[:160]}) -- failing over to '{remaining[0]}'",
                    flush=True,
                )
                continue
            print(f"[llm] provider '{name}' unavailable and no fallback left", flush=True)
            raise
        except LLMError as e:
            # Hard error (bad response shape, unknown provider). Still worth
            # trying the next link rather than dropping the player's turn.
            last_error = e
            remaining = chain[idx + 1:]
            if remaining:
                print(
                    f"[llm] provider '{name}' errored ({e}) -- "
                    f"failing over to '{remaining[0]}'",
                    flush=True,
                )
                continue
            raise

    raise LLMError(f"No provider in chain {chain} could serve the request: {last_error}")


async def generate_with(
    http: httpx.AsyncClient,
    messages: List[dict],
    *,
    backend: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    """Generate using one named provider, with no failover."""
    p = _provider(backend)
    limit = max_tokens if max_tokens is not None else settings.max_output_tokens
    temp = temperature if temperature is not None else OLLAMA_OPTS["temperature"]

    if p.kind == "ollama":
        return await _generate_ollama(http, p, messages, limit, temp)
    return await _generate_openai_compatible(http, p, messages, limit, temp)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

async def _generate_ollama(
    http: httpx.AsyncClient,
    p: Provider,
    messages: List[dict],
    max_tokens: int,
    temperature: float,
) -> str:
    opts = {
        **OLLAMA_OPTS,
        "temperature": temperature,
        "num_predict": max_tokens,
        "num_ctx": settings.num_ctx,
        "num_thread": settings.num_thread,
    }

    r = await http.post(
        f"{p.base_url.rstrip('/')}/api/chat",
        json={
            "model": p.model,
            "messages": messages,
            "stream": False,
            "options": opts,
        },
        timeout=settings.gen_timeout,
    )

    if r.status_code >= 500:
        raise ProviderUnavailable(f"ollama returned {r.status_code}")
    if r.status_code >= 400:
        raise LLMError(f"ollama returned {r.status_code}: {r.text[:200]}")

    d = r.json()
    if isinstance(d, dict):
        msg = d.get("message") or {}
        if isinstance(msg, dict) and msg.get("content"):
            return msg["content"]
        if d.get("response"):
            return d["response"]

    raise LLMError(f"Unexpected Ollama chat response: {str(d)[:300]}")


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------

# Status codes meaning "try someone else" rather than "this request is bad".
_FAILOVER_STATUS = {401, 402, 403, 408, 409, 425, 429, 500, 502, 503, 504, 529}


async def _generate_openai_compatible(
    http: httpx.AsyncClient,
    p: Provider,
    messages: List[dict],
    max_tokens: int,
    temperature: float,
) -> str:
    if not p.api_key:
        raise ProviderUnavailable(
            f"{p.name}: API key not set (put it in the EnvironmentFile, not git)"
        )
    if not p.model:
        raise ProviderUnavailable(f"{p.name}: model id not set")

    url = f"{p.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {p.api_key}",
        "Content-Type": "application/json",
        **p.headers,
    }
    body = {
        "model": p.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    r = await http.post(url, json=body, headers=headers, timeout=settings.hosted_timeout)

    # Newer OpenAI model families reject `max_tokens` (requiring
    # `max_completion_tokens`) and some reject a non-default temperature. Both
    # surface as 400s. Retry once with adjusted params so a model swap does not
    # require a code change.
    if r.status_code == 400:
        detail = r.text[:500]
        retry = dict(body)
        changed = False
        if "max_completion_tokens" in detail or "max_tokens" in detail:
            retry.pop("max_tokens", None)
            retry["max_completion_tokens"] = max_tokens
            changed = True
        if "temperature" in detail:
            retry.pop("temperature", None)
            changed = True
        if changed:
            print(f"[llm] {p.name}: retrying with adjusted params after 400: "
                  f"{detail[:160]}", flush=True)
            r = await http.post(url, json=retry, headers=headers,
                                timeout=settings.hosted_timeout)

    if r.status_code in _FAILOVER_STATUS:
        raise ProviderUnavailable(f"{p.name} returned {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise LLMError(f"{p.name} returned {r.status_code}: {r.text[:300]}")

    d = r.json()

    # OpenRouter surfaces upstream failures as 200 + an error envelope.
    if isinstance(d, dict) and d.get("error") and not d.get("choices"):
        raise ProviderUnavailable(f"{p.name} error envelope: {str(d['error'])[:200]}")

    try:
        content = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"Unexpected {p.name} response: {str(d)[:300]}")

    if not content:
        # A reasoning model can spend its whole budget on hidden reasoning
        # tokens and emit no visible text.
        raise ProviderUnavailable(
            f"{p.name} returned an empty completion "
            "(if this is a reasoning model, raise MAX_OUTPUT_TOKENS)"
        )

    return content
