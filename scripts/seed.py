"""Create a demo API client + knowledge base and ingest the sample documents.

    uv run poe seed

Requires the stack to be running (``uv run poe up``). Uses the bootstrap admin key from
.env, prints the new demo API key, and shows ready-to-paste curl commands.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

from rag_service.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8000"
QUERY = "http://127.0.0.1:8001"


def main() -> None:
    admin_key = Settings().bootstrap_admin_key
    if admin_key is None:
        sys.exit("RAG_BOOTSTRAP_ADMIN_KEY is not set in .env")
    admin = httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {admin_key.get_secret_value()}"},
        timeout=30,
    )

    client = admin.post("/v1/api-clients", json={"name": "demo-app"})
    client.raise_for_status()
    key = client.json()["api_key"]
    app = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=60)

    kb = app.post("/v1/knowledge-bases", json={"name": f"demo-{int(time.time())}"})
    kb.raise_for_status()
    kb_id = kb.json()["id"]
    print(f"Knowledge base: {kb_id}")

    doc_ids = []
    for path in sorted((ROOT / "examples" / "sample_docs").glob("*")):
        with path.open("rb") as fh:
            r = app.post(
                f"/v1/knowledge-bases/{kb_id}/documents",
                files={"file": (path.name, fh)},
                data={"external_id": path.stem},
            )
        r.raise_for_status()
        doc_ids.append(r.json()["document"]["id"])
        print(f"  uploaded {path.name}")

    print("Waiting for ingestion", end="", flush=True)
    for doc_id in doc_ids:
        for _ in range(150):
            status = app.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}").json()["status"]
            if status in ("ready", "failed"):
                break
            print(".", end="", flush=True)
            time.sleep(2)
        print(f" {status}", end="")
    print()

    print(f"""
Demo API key (shown once): {key}

Try it:
  curl -s {QUERY}/v1/query -H "Authorization: Bearer {key}" -H "Content-Type: application/json" \\
       -d '{{"kb_ids": ["{kb_id}"], "query": "How many days of annual leave do I get?"}}'

  curl -N {QUERY}/v1/query -H "Authorization: Bearer {key}" -H "Content-Type: application/json" \\
       -d '{{"kb_ids": ["{kb_id}"], "query": "How do I report phishing?", "stream": true}}'
""")


if __name__ == "__main__":
    main()
