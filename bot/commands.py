# bot/commands.py
from typing import Optional

import discord
import requests
from discord import app_commands

from config import BACKEND_URL, get_headers
from helpers import check_health, check_deep_health, sync_player

# -------------------------------------------------
# Slash Commands (grouped under /gn)
# -------------------------------------------------
gn = app_commands.Group(name="gn", description="GhostNet control and debug commands")


@gn.command(name="status", description="Check GhostNet backend health.")
async def gn_status(interaction: discord.Interaction):
    ok, data = check_health()

    from presence import update_presence_from_health  # local import to avoid cycles
    await update_presence_from_health(interaction.client)  # type: ignore[arg-type]

    if ok:
        st = data.get("status", "unknown")
        emoji = "🟢" if st == "ok" else "🟡"
        msg = f"{emoji} **ghostnet-api** health: **{st}**\n🌐 `{BACKEND_URL}/health`"
    else:
        err = data
        msg = f"🔴 ghostnet-api health: DOWN\n`{type(err).__name__}: {err}`"

    await interaction.response.send_message(msg)


@gn.command(name="deepstatus", description="Deep health check (DB, Qdrant, Ollama).")
async def gn_deepstatus(interaction: discord.Interaction):
    ok, data = check_deep_health()

    from presence import update_presence_from_health  # local import
    await update_presence_from_health(interaction.client)  # type: ignore[arg-type]

    if not ok:
        err = data
        await interaction.response.send_message(
            f"🔴 ghostnet-api deep health: DOWN\n`{type(err).__name__}: {err}`"
        )
        return

    st = data.get("status", "unknown")
    comps = data.get("components", {})

    def line(name: str) -> str:
        c = comps.get(name, {})
        if c.get("ok"):
            return f"🟢 {name}"
        else:
            err = c.get("error")
            return f"🔴 {name} — `{err}`"

    msg = (
        f"**ghostnet-api deep status: {st}**\n"
        f"{line('db')}\n"
        f"{line('qdrant')}\n"
        f"{line('ollama')}\n"
        f"🌐 `{BACKEND_URL}/health/deep`"
    )

    await interaction.response.send_message(msg)


@gn.command(name="search", description="RAG debug search.")
@app_commands.describe(query="Query to search inside GhostNet RAG knowledge.")
async def gn_search(interaction: discord.Interaction, query: str):
    payload = {"query": query, "top_k": 3}

    try:
        resp = requests.post(
            f"{BACKEND_URL}/search",
            json=payload,
            headers=get_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        await interaction.response.send_message(
            f"⚠️ Search error: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )
        return

    results = data.get("results", [])
    if not results:
        await interaction.response.send_message(
            f"🔍 No RAG hits for `{query}`",
            ephemeral=True,
        )
        return

    lines = [f"🔍 **RAG search results for:** `{query}`"]

    for i, hit in enumerate(results[:3], start=1):
        text = str(hit.get("text", "")).replace("```", "ʼʼʼ")
        score = hit.get("score", 0.0)
        if len(text) > 280:
            text = text[:277] + "..."
        lines.append(f"**{i}.** *(score {score:.3f})*\n> {text}")

    await interaction.response.send_message("\n\n".join(lines))


# --- World document commands --------------------------------------------
@gn.command(name="world_add", description="Add a world document into the Overworld Nexus.")
@app_commands.describe(
    world="World/setting name (e.g. overworld)",
    kind="Document type (e.g. lore, character, location)",
    title="Short title for this document",
    body="Full body text",
    tags="Optional comma-separated tags",
)
async def gn_world_add(
    interaction: discord.Interaction,
    world: str,
    kind: str,
    title: str,
    body: str,
    tags: Optional[str] = None,
):
    await interaction.response.defer(thinking=True, ephemeral=True)

    tag_list = [t.strip() for t in tags.split(",")] if tags else []

    payload = [
        {
            "world": world,
            "kind": kind,
            "title": title,
            "body": body,
            "tags": tag_list,
            "status": "active",
            "created_by": str(interaction.user.id),
            "created_from_message_id": str(interaction.id),
        }
    ]

    try:
        resp = requests.post(
            f"{BACKEND_URL}/world/docs",
            json=payload,
            headers=get_headers(),
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Error adding world doc: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )
        return

    inserted = data.get("inserted")
    ids = data.get("ids", [])
    msg = f"✅ Inserted **{inserted}** world document(s)."
    if ids:
        msg += f" IDs: `{', '.join(str(i) for i in ids)}`"

    await interaction.followup.send(msg, ephemeral=True)


@gn.command(name="world_list", description="List recent world documents.")
@app_commands.describe(
    world="Filter by world (optional)",
    kind="Filter by kind (optional)",
    tag="Filter by tag (optional)",
    limit="Max docs to show (default 10)",
)
async def gn_world_list(
    interaction: discord.Interaction,
    world: Optional[str] = None,
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 10,
):
    await interaction.response.defer(thinking=True, ephemeral=True)

    params: dict[str, str | int] = {"limit": limit}
    if world:
        params["world"] = world
    if kind:
        params["kind"] = kind
    if tag:
        params["tag"] = tag

    try:
        resp = requests.get(
            f"{BACKEND_URL}/world/docs",
            headers=get_headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        docs = resp.json()
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Error listing world docs: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )
        return

    if not docs:
        await interaction.followup.send(
            "📭 No world documents found for that filter.",
            ephemeral=True,
        )
        return

    lines = ["📜 **World documents:**"]
    for d in docs[:limit]:
        tags_str = ", ".join(d.get("tags") or []) if isinstance(d.get("tags"), list) else str(d.get("tags"))
        lines.append(
            f"- `#{d.get('id')}` **{d.get('title')}** "
            f"(_{d.get('world')}/{d.get('kind')}_; tags: {tags_str})"
        )

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@gn.command(name="world_get", description="Fetch a world document by ID.")
@app_commands.describe(doc_id="World document ID")
async def gn_world_get(interaction: discord.Interaction, doc_id: int):
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        resp = requests.get(
            f"{BACKEND_URL}/world/docs/{doc_id}",
            headers=get_headers(),
            timeout=15,
        )
        if resp.status_code == 404:
            await interaction.followup.send(
                f"❌ World document `#{doc_id}` not found.",
                ephemeral=True,
            )
            return
        resp.raise_for_status()
        doc = resp.json()
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Error fetching world doc: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )
        return

    title = doc.get("title", "(no title)")
    world = doc.get("world", "?")
    kind = doc.get("kind", "?")
    tags = doc.get("tags")
    tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
    body = doc.get("body", "")

    if len(body) > 900:
        body_display = body[:897] + "..."
    else:
        body_display = body

    msg = (
        f"📄 **World doc `#{doc_id}` — {title}**\n"
        f"_World_: **{world}**, _Kind_: **{kind}**, _Tags_: {tags_str}\n\n"
        f"```text\n{body_display}\n```"
    )

    await interaction.followup.send(msg, ephemeral=True)


# --- Player / lore commands ---------------------------------------------
@gn.command(name="whois", description="Show GhostNet dossier for a player.")
@app_commands.describe(
    target="Discord user (optional, default: yourself)",
    handle="Name / alias text search if user not specified.",
)
async def gn_whois(
    interaction: discord.Interaction,
    target: Optional[discord.Member] = None,
    handle: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)

    if target is not None:
        member = target
    else:
        member = interaction.user

    if isinstance(member, discord.Member):
        sync_player(member)

    try:
        if handle and not target:
            resp = requests.get(
                f"{BACKEND_URL}/players/by_handle/{handle}",
                headers=get_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            players = resp.json()
            if not players:
                await interaction.followup.send(
                    f"❓ No players found matching handle `{handle}`.",
                    ephemeral=True,
                )
                return
            p = players[0]
        else:
            resp = requests.get(
                f"{BACKEND_URL}/players/{member.id}",
                headers=get_headers(),
                timeout=10,
            )
            if resp.status_code == 404:
                await interaction.followup.send(
                    "❓ No player record found yet. "
                    "Say something in chat so GhostNet can register you.",
                    ephemeral=True,
                )
                return
            resp.raise_for_status()
            p = resp.json()
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Error fetching player info: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )
        return

    aliases = p.get("aliases") or []
    aliases_str = ", ".join(aliases) if aliases else "_none yet_"

    msg = (
        f"🧾 **GhostNet dossier**\n"
        f"ID: `{p.get('id')}`\n"
        f"Discord: <@{p.get('discord_id')}>\n"
        f"Primary handle: **{p.get('primary_handle')}**\n"
        f"Display name: **{p.get('display_name') or '—'}**\n"
        f"NPC: **{p.get('is_npc')}**\n"
        f"Aliases: {aliases_str}"
    )

    await interaction.followup.send(msg, ephemeral=True)


@gn.command(name="arc", description="Generate an in-world story arc for a player.")
@app_commands.describe(
    target="Discord user (optional, default: yourself)",
)
async def gn_arc(
    interaction: discord.Interaction,
    target: Optional[discord.Member] = None,
):
    await interaction.response.defer(ephemeral=True)

    member = target or interaction.user

    if isinstance(member, discord.Member):
        sync_player(member)

    try:
        resp = requests.get(
            f"{BACKEND_URL}/players/{member.id}",
            headers=get_headers(),
            timeout=10,
        )
        if resp.status_code == 404:
            await interaction.followup.send(
                "❓ No player record found yet. "
                "Say something in chat so GhostNet can register you.",
                ephemeral=True,
            )
            return
        resp.raise_for_status()
        p = resp.json()
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Error fetching player info: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )
        return

    prompt = (
        "You are GhostNet Daemon narrating the Overworld Nexus mesh.\n"
        "Based on the structured player profile below, write a short in-world "
        "dossier + story arc hook for this character. 3–5 paragraphs max, "
        "presented as if from an in-universe systems log. "
        "If details are missing, lean into the mystery and hint that the echoes "
        "are still compiling more data.\n\n"
        f"Player profile:\n"
        f"- primary_handle: {p.get('primary_handle')}\n"
        f"- display_name: {p.get('display_name')}\n"
        f"- is_npc: {p.get('is_npc')}\n"
        f"- aliases: {', '.join(p.get('aliases') or [])}\n"
    )

    chat_payload = {
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "rag": True,
    }

    try:
        resp = requests.post(
            f"{BACKEND_URL}/chat",
            json=chat_payload,
            headers=get_headers(),
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("content") or str(data)
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Error generating arc: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )
        return

    if len(text) > 1900:
        text = text[:1897] + "..."

    await interaction.followup.send(f"📡 **Arc for <@{p.get('discord_id')}>**\n\n{text}", ephemeral=True)


def register_gn_commands(tree: app_commands.CommandTree) -> None:
    """
    Attach the /gn group (and all subcommands) to the given CommandTree.
    """
    tree.add_command(gn)
