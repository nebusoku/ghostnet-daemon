import os
import json
import requests

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001")
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "change-me")

HEADERS = {"Authorization": f"Bearer {BACKEND_API_KEY}"}

EXPORT_FILE = os.path.expanduser("~/ghostnet_qdrant_export.jsonl")

# Defaults for world docs
DEFAULT_WORLD = "overworld"
DEFAULT_KIND = "lore"


def make_title(text: str, max_len: int = 80) -> str:
    text = text.strip().splitlines()[0]
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def main():
    if not os.path.exists(EXPORT_FILE):
        print(f"Export file not found: {EXPORT_FILE}")
        return

    docs = []
    with open(EXPORT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)

            text = obj.get("text", "").strip()
            if not text:
                continue

            tags = obj.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]

            title = make_title(text)

            docs.append(
                {
                    "world": DEFAULT_WORLD,
                    "kind": DEFAULT_KIND,
                    "title": title,
                    "body": text,
                    "tags": tags,
                    "status": "active",
                    "created_by": "macmini-qdrant",
                    "created_from_message_id": None,
                }
            )

    if not docs:
        print("No docs to import.")
        return

    print(f"Prepared {len(docs)} docs to import into /world/docs")

    batch_size = 25
    inserted_total = 0
    ids_all = []

    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        print(f"Sending batch {i // batch_size + 1} ({len(batch)} docs)...")

        resp = requests.post(
            f"{BACKEND_URL}/world/docs",
            json=batch,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        inserted_total += data.get("inserted", 0)
        ids_all.extend(data.get("ids", []))

    print(f"Imported {inserted_total} docs. Sample IDs: {ids_all[:10]}")


if __name__ == "__main__":
    main()
