import os
import requests
import discord
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001")
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "change-me")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in environment or .env")

# Discord intents
intents = discord.Intents.default()
intents.message_content = True  # needed to read message content

# Create the client
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"GhostNet Daemon logged in as {client.user} (ID: {client.user.id})")


@client.event
async def on_message(message: discord.Message):
    # Ignore messages from bots (including ourselves)
    if message.author.bot:
        return

    # Basic payload – matches simple ChatRequest style (adjust later if needed)
    payload = {
        "messages": [
            {
                "role": "user",
                "content": message.content,
            }
        ]
    }

    headers = {
        "Authorization": f"Bearer {BACKEND_API_KEY}"
    }

    try:
        resp = requests.post(
            f"{BACKEND_URL}/chat",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # Adjust this depending on your ChatResponse schema
        reply_text = data.get("answer") or data.get("reply") or str(data)
    except Exception as e:
        reply_text = f"GhostNet backend error: {e}"

    await message.channel.send(reply_text)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
