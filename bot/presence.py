# bot/presence.py
import discord

from helpers import check_health


async def update_presence_from_health(client: discord.Client) -> None:
    """Update Discord presence based on backend health."""
    ok, data = check_health()
    try:
        if ok:
            status_value = data.get("status", "unknown")
            if status_value == "ok":
                await client.change_presence(
                    status=discord.Status.online,
                    activity=discord.Game("GhostNet: online"),
                )
            else:
                await client.change_presence(
                    status=discord.Status.idle,
                    activity=discord.Game(f"GhostNet: {status_value}"),
                )
        else:
            await client.change_presence(
                status=discord.Status.dnd,
                activity=discord.Game("GhostNet: backend DOWN"),
            )
    except Exception:
        # Don't crash the bot if presence update fails
        pass
