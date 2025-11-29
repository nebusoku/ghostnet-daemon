# bot/helpers.py
from typing import Tuple, Any

import discord
import requests

from config import BACKEND_URL, get_headers


def sync_player(member: discord.Member) -> None:
    """
    Best-effort sync of a Discord member into /players.
    """
    if member is None:
        return

    if member.bot:
        return

    primary_handle = getattr(member, "global_name", None) or member.name
    display_name = member.display_name

    try:
        avatar_url = member.display_avatar.url
    except Exception:
        avatar_url = None

# Detect Carl-bot / server role for adult content opt-in
    mature_ok = False
    try:
        for role in getattr(member, "roles", []):
            if role.name == MATURE_ROLE_NAME:
                mature_ok = True
                break
    except Exception:
        mature_ok = False

    payload = {
        "discord_id": str(member.id),
        "primary_handle": primary_handle or display_name or str(member.id),
        "display_name": display_name,
        "avatar_url": avatar_url,
        "is_npc": False,
        "aliases": [],
        "mature_ok": mature_ok,
    }

    try:
        resp = requests.post(
            f"{BACKEND_URL}/players",
            json=payload,
            headers=get_headers(),
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[player-sync] Failed to sync {member} ({member.id}): {e}", flush=True)


def check_health() -> Tuple[bool, Any]:
    """Lightweight /health check."""
    try:
        resp = requests.get(
            f"{BACKEND_URL}/health",
            headers=get_headers(),
            timeout=5,
        )
        resp.raise_for_status()
        return True, resp.json()
    except Exception as e:
        return False, e


def check_deep_health() -> Tuple[bool, Any]:
    """/health/deep check (DB, Qdrant, Ollama)."""
    try:
        resp = requests.get(
            f"{BACKEND_URL}/health/deep",
            headers=get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return True, resp.json()
    except Exception as e:
        return False, e
