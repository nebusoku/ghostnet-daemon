# bot/bot.py
import asyncio
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import tasks
import requests

from config import (
    DISCORD_TOKEN,
    PRIMARY_GUILD_ID,
    BACKEND_URL,
    CHAT_TIMEOUT,
    get_headers,
)
from helpers import check_health, check_deep_health, sync_player
from channels import get_servercontrol_channel, get_heartbeat_channel
from presence import update_presence_from_health
from chunking import DISCORD_SAFE_LEN, split_for_discord
from commands import register_gn_commands

print("🔥 GhostNet bot.py loaded and executing…", flush=True)

# -------------------------------------------------
# Discord client setup
# -------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Register grouped /gn commands
register_gn_commands(tree)


def _cmd_debug_name(cmd) -> str:
    """Safely get a readable name for an app command or group."""
    return getattr(cmd, "qualified_name", getattr(cmd, "name", repr(cmd)))


# -------------------------------------------------
# Async-safe helpers (avoid blocking Discord loop)
# -------------------------------------------------
_HTTP_MAX_CONCURRENCY = 8
_http_sem = asyncio.Semaphore(_HTTP_MAX_CONCURRENCY)


async def _to_thread(fn: Callable[..., Any], *args, **kwargs) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


async def http_get(url: str, *, headers: dict, timeout: int) -> requests.Response:
    async with _http_sem:
        return await _to_thread(requests.get, url, headers=headers, timeout=timeout)


async def http_post(
    url: str, *, json: dict, headers: dict, timeout: int
) -> requests.Response:
    async with _http_sem:
        return await _to_thread(
            requests.post, url, json=json, headers=headers, timeout=timeout
        )


async def run_blocking(fn: Callable[..., Any], *args, **kwargs) -> Any:
    """
    Run a blocking helper (like check_health/sync_player) without blocking the event loop.
    Uses the same semaphore as HTTP to avoid runaway concurrency under load.
    """
    async with _http_sem:
        return await _to_thread(fn, *args, **kwargs)


# -------------------------------------------------
# Discord message chunking (daemon replies can exceed 2k chars)
# -------------------------------------------------
async def send_discord_chunked(
    channel: discord.abc.Messageable, text: str, limit: int = DISCORD_SAFE_LEN
) -> None:
    """Send text in ordered chunks under Discord message limits."""
    chunks = split_for_discord(text, limit=limit)
    if not chunks:
        return

    for i, chunk in enumerate(chunks):
        await channel.send(chunk)
        # small pause reduces rate-limit risk when sending many chunks
        if i < len(chunks) - 1:
            await asyncio.sleep(0.35)


# -------------------------------------------------
# Events
# -------------------------------------------------
@client.event
async def on_ready():
    print(f"✅ on_ready fired for {client.user} (ID: {client.user.id})", flush=True)

    await update_presence_from_health(client)

    # Debug: what commands do we have BEFORE sync?
    all_cmds = tree.get_commands()
    print(
        f"📜 CommandTree has {len(all_cmds)} commands before sync: "
        f"{[_cmd_debug_name(c) for c in all_cmds]}",
        flush=True,
    )

    try:
        if PRIMARY_GUILD_ID:
            guild_obj = discord.Object(id=int(PRIMARY_GUILD_ID))
            # copy global commands into that guild, then sync
            tree.copy_global_to(guild=guild_obj)
            print(f"🔧 Syncing commands to guild {PRIMARY_GUILD_ID}...", flush=True)
            synced = await tree.sync(guild=guild_obj)
            print(
                f"✅ Synced {len(synced)} commands to guild {PRIMARY_GUILD_ID}: "
                f"{[_cmd_debug_name(c) for c in synced]}",
                flush=True,
            )
        else:
            print("🔧 Syncing GLOBAL commands...", flush=True)
            synced = await tree.sync()
            print(
                f"✅ Synced {len(synced)} global commands: "
                f"{[_cmd_debug_name(c) for c in synced]}",
                flush=True,
            )
    except Exception as e:
        print(f"❌ Slash command sync error: {e}", flush=True)

    if not heartbeat_task.is_running():
        heartbeat_task.start()
        print("❤️ Heartbeat task started.", flush=True)


# -------------------------------------------------
# Heartbeat Loop (every 6 hours)
# -------------------------------------------------
HEARTBEAT_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours


@tasks.loop(seconds=HEARTBEAT_INTERVAL_SECONDS)
async def heartbeat_task():
    """Periodic health ping to #heartbeat + presence update."""
    ch = await get_heartbeat_channel(client)

    ok, data = await run_blocking(check_health)
    await update_presence_from_health(client)

    if ch is None:
        return

    if ok:
        status = data.get("status", "unknown")
        emoji = "🟢" if status == "ok" else "🟡"
        msg = (
            f"{emoji} **ghostnet-api** heartbeat: **{status}**\n"
            f"🌐 `{BACKEND_URL}/health`"
        )
    else:
        err = data
        msg = (
            "🔴 **ghostnet-api** heartbeat: **DOWN**\n"
            f"⚠️ `{type(err).__name__}: {err}`"
        )

    await ch.send(msg)


# -------------------------------------------------
# Message-based Commands + Chat
# -------------------------------------------------
@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Keep player record synced (avoid blocking loop)
    await run_blocking(sync_player, message.author)

    content = message.content.strip()

    # ----- STATUS COMMAND -----
    if content.lower() in ("!status", "/status"):
        ch = await get_servercontrol_channel(client)
        if ch is None:
            await message.channel.send(
                "⚠️ Cannot find #servercontrol (ID=None, name='servercontrol')"
            )
            return

        ok, data = await run_blocking(check_health)
        await update_presence_from_health(client)

        if ok:
            status = data.get("status", "unknown")
            emoji = "🟢" if status == "ok" else "🟡"
            msg = (
                f"{emoji} **ghostnet-api** health: **{status}**\n"
                f"🌐 `{BACKEND_URL}/health`\n"
                f"🔈 requested by {message.author.mention}"
            )
        else:
            err = data
            msg = (
                "🔴 **ghostnet-api** health: **DOWN**\n"
                f"⚠️ `{type(err).__name__}: {err}`\n"
                f"🔈 requested by {message.author.mention}"
            )

        await ch.send(msg)
        return

    # ----- DEEPSTATUS COMMAND -----
    if content.lower() in ("!deepstatus", "/deepstatus"):
        ch = await get_servercontrol_channel(client)
        if ch is None:
            await message.channel.send(
                "⚠️ Cannot find #servercontrol (ID=None, name='servercontrol')"
            )
            return

        ok, data = await run_blocking(check_deep_health)
        await update_presence_from_health(client)

        if not ok:
            err = data
            msg = (
                "🔴 **ghostnet-api deep health: DOWN**\n"
                f"⚠️ `{type(err).__name__}: {err}`\n"
                f"🔈 requested by {message.author.mention}"
            )
            await ch.send(msg)
            return

        status = data.get("status", "unknown")
        comps = data.get("components", {})

        def fmt_component(name: str) -> str:
            c = comps.get(name, {})
            if c.get("ok"):
                return f"🟢 {name}"
            else:
                err = c.get("error")
                if err:
                    return f"🔴 {name} — `{err}`"
                else:
                    return f"🔴 {name} — unknown error"

        db_line = fmt_component("db")
        qd_line = fmt_component("qdrant")
        ol_line = fmt_component("ollama")

        emoji = "🟢" if status == "ok" else ("🟡" if status == "degraded" else "🔴")

        msg = (
            f"{emoji} **ghostnet-api deep status: {status}**\n"
            f"{db_line}\n"
            f"{qd_line}\n"
            f"{ol_line}\n"
            f"🌐 `{BACKEND_URL}/health/deep`\n"
            f"🔈 requested by {message.author.mention}"
        )

        await ch.send(msg)
        return

    # ----- SEARCH DEBUG COMMAND -----
    if content.lower().startswith("!search") or content.lower().startswith("/search "):
        parts = content.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.channel.send(
                "Usage: `!search <query>` — shows top RAG hits."
            )
            return

        query = parts[1].strip()

        payload = {
            "query": query,
            "top_k": 3,
        }

        try:
            resp = await http_post(
                f"{BACKEND_URL}/search",
                json=payload,
                headers=get_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            await message.channel.send(f"⚠️ Search error: `{type(e).__name__}: {e}`")
            return

        results = data.get("results", [])
        if not results:
            await message.channel.send(f"🔍 No RAG hits for: `{query}`")
            return

        lines = [f"🔍 **RAG search results for:** `{query}`"]

        for i, hit in enumerate(results[:3], start=1):
            text = str(hit.get("text", "")).replace("```", "ʼʼʼ")
            score = hit.get("score", 0.0)
            if len(text) > 280:
                text = text[:277] + "..."
            lines.append(f"**{i}.** *(score {score:.3f})*\n> {text}")

        msg = "\n\n".join(lines)
        await message.channel.send(msg)
        return

    # ----- NORMAL CHAT FLOW -----
    # Look up player to get mature_ok and feed as a system hint
    mature_hint = "SYSTEM: player_mature_ok=false"
    try:
        resp = await http_get(
            f"{BACKEND_URL}/players/{message.author.id}",
            headers=get_headers(),
            timeout=5,
        )
        if resp.status_code == 200:
            p = resp.json()
            if p.get("mature_ok"):
                mature_hint = "SYSTEM: player_mature_ok=true"
    except Exception:
        # if lookup fails, default stays "false"
        pass

    payload = {
        "system": mature_hint,
        # clean_content resolves <@123456789> mentions to readable display
        # names, <#id> to channel names and <@&id> to role names. Sending raw
        # content means the model sees bare snowflakes and echoes them back at
        # players -- the daemon referring to "227145716458323979" instead of
        # "FossilizedFibonacci".
        "messages": [{"role": "user", "content": message.clean_content}],
        # Memory binding. The channel groups turns into a scene; the author id
        # is the identity the world tracks across channels and absences.
        # Only the newest turn is sent -- the API assembles history from its
        # own store within a token budget, so prompt cost stays flat however
        # long a scene runs.
        "conversation_id": str(message.channel.id),
        "discord_id": str(message.author.id),
        "source": "discord",
    }

    try:
        async with message.channel.typing():
            resp = await http_post(
                f"{BACKEND_URL}/chat",
                json=payload,
                headers=get_headers(),
                timeout=CHAT_TIMEOUT,
            )
        resp.raise_for_status()
        data = resp.json()
        reply_text = data.get("content") or str(data)

    except requests.exceptions.ReadTimeout:
        reply_text = (
            "System Log: local mesh channel stalled while querying the Overworld Nexus core. "
            "This echo timed out before a clean response came back. "
            "Try again with a tighter query or after the cores settle."
        )
    except Exception as e:
        reply_text = f"GhostNet backend error: {e}"

    # Send in ordered sentence-rounded chunks to stay under Discord message limits.
    await send_discord_chunked(message.channel, reply_text)


# -------------------------------------------------
# Entrypoint
# -------------------------------------------------
if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
