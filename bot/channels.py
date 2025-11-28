# bot/channels.py
from typing import Optional

import discord

from config import (
    SERVERCONTROL_CHANNEL_ID,
    SERVERCONTROL_CHANNEL_NAME,
    HEARTBEAT_CHANNEL_ID,
    HEARTBEAT_CHANNEL_NAME,
)


async def _fetch_channel_by_id(
    client: discord.Client,
    cid: Optional[str],
) -> Optional[discord.TextChannel]:
    """Fetch channel by ID if possible."""
    if not cid:
        return None
    try:
        ch = await client.fetch_channel(int(cid))
        if isinstance(ch, discord.TextChannel):
            return ch
    except Exception:
        return None
    return None


async def _find_channel_by_name(
    client: discord.Client,
    name: str,
) -> Optional[discord.TextChannel]:
    """Find a channel by name across all guilds."""
    for guild in client.guilds:
        for ch in guild.text_channels:
            if ch.name == name:
                return ch
    return None


async def get_servercontrol_channel(client: discord.Client) -> Optional[discord.TextChannel]:
    ch = await _fetch_channel_by_id(client, SERVERCONTROL_CHANNEL_ID)
    if ch:
        return ch
    return await _find_channel_by_name(client, SERVERCONTROL_CHANNEL_NAME)


async def get_heartbeat_channel(client: discord.Client) -> Optional[discord.TextChannel]:
    ch = await _fetch_channel_by_id(client, HEARTBEAT_CHANNEL_ID)
    if ch:
        return ch
    return await _find_channel_by_name(client, HEARTBEAT_CHANNEL_NAME)
