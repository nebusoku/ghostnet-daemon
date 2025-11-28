import os
import discord
import requests
import asyncio
from discord import Intents

# ---------------------------------------------
# Load ENV
# ---------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001")
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "change-me")

CHANNEL_IDS = os.getenv("DISCORD_LORE_CHANNEL_IDS", "")
WORLD = os.getenv("LORE_WORLD_NAME", "overworld")
KIND = os.getenv("LORE_KIND_NAME", "discord-lore")
MIN_LEN = int(os.getenv("DISCORD_MIN_LORE_LEN", "40"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN must be set.")


# ---------------------------------------------
# Discord Client
# ---------------------------------------------
intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

HEADERS = {"Authorization": f"Bearer {BACKEND_API_KEY}"}


async def ingest_message_batch(batch):
    """Send a batch of world docs to /world/docs"""
    if not batch:
        return 0

    try:
        resp = requests.post(
            f"{BACKEND_URL}/world/docs",
            json=batch,
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"Inserted batch: {data}")
        return data.get("inserted", 0)
    except Exception as e:
        print(f"Batch error: {e}")
        return 0


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    channel_ids = [int(cid) for cid in CHANNEL_IDS.split(",") if cid.strip()]
    total = 0

    for cid in channel_ids:
        chan = client.get_channel(cid)
        if not chan:
            print(f"Cannot find channel {cid}")
            continue

        print(f"Processing channel {cid} ({chan.name})…")
        batch = []

        async for msg in chan.history(limit=None, oldest_first=True):

            # Skip pure non-text (embeds, stickers, attachments-only)
            content = msg.content.strip()
            if not content:
                continue

            if len(content) < MIN_LEN:
                continue

            author_type = "bot" if msg.author.bot else "user"

            doc = {
                "world": WORLD,
                "kind": KIND,
                "title": f"discord-{chan.name}-{msg.id}",
                "body": f"[{author_type}] {content}",
                "tags": ["discord", chan.name, author_type],
                "status": "active",
                "created_by": str(msg.author.id),
                "created_from_message_id": str(msg.id),
            }

            batch.append(doc)

            # Batch flush
            if len(batch) >= 50:
                total += await ingest_message_batch(batch)
                batch = []

        # final flush
        total += await ingest_message_batch(batch)

    print(f"Done ingesting. Total docs inserted: {total}")
    await client.close()


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
