"""End-to-end flow against the running Compose stack (IMPLEMENTATION_PLAN.md P1.24).

Start the stack wired to the fake OpenAI server, then run::

    uv run poe e2e-up
    uv run poe e2e

The admin key comes from .env (``RAG_BOOTSTRAP_ADMIN_KEY``). Every run creates
uniquely named KBs, so runs don't interfere with each other or with real data.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from rag_service.core.config import Settings

pytestmark = pytest.mark.e2e

API = os.environ.get("E2E_API_URL", "http://127.0.0.1:8000")
QUERY = os.environ.get("E2E_QUERY_URL", "http://127.0.0.1:8001")

HANDBOOK = """# Employee Handbook

## Leave

Full-time employees receive 24 days of paid annual leave per calendar year.
Unused leave can be carried over until March 31.

## Expenses

Travel expenses must be submitted within 30 days through the finance portal.
"""

SECURITY = b"""Security policy.

Laptops must use full-disk encryption. Passwords rotate every 90 days.
Report phishing emails to the security team immediately.
"""


@pytest.fixture(scope="module")
def admin() -> Iterator[httpx.Client]:
    key = Settings().bootstrap_admin_key
    if key is None or not key.get_secret_value():
        pytest.skip("RAG_BOOTSTRAP_ADMIN_KEY is not set in .env")
    headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
    with httpx.Client(base_url=API, headers=headers, timeout=30) as client:
        try:
            client.get("/healthz").raise_for_status()
        except httpx.HTTPError:
            pytest.skip(f"Stack not reachable at {API}; run `uv run poe e2e-up` first")
        yield client


def _app_client(admin: httpx.Client, **body: Any) -> str:
    response = admin.post("/v1/api-clients", json={"name": f"e2e-{uuid.uuid4().hex[:6]}", **body})
    assert response.status_code == 201, response.text
    return str(response.json()["api_key"])


def _wait_ready(
    client: httpx.Client, kb_id: str, doc_id: str, timeout_s: float = 240
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        doc: dict[str, Any] = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}").json()
        if doc["status"] in ("ready", "failed"):
            return doc
        time.sleep(2)
    pytest.fail(f"document {doc_id} not ready after {timeout_s}s")


def _sse_events(response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    events, name = [], None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and name:
            events.append((name, json.loads(line.split(":", 1)[1])))
            name = None
    return events


def test_full_rag_flow(admin: httpx.Client) -> None:
    key = _app_client(admin)
    headers = {"Authorization": f"Bearer {key}"}
    with (
        httpx.Client(base_url=API, headers=headers, timeout=30) as app,
        httpx.Client(base_url=QUERY, headers=headers, timeout=60) as q,
    ):
        # --- knowledge base
        kb = app.post("/v1/knowledge-bases", json={"name": f"e2e-{uuid.uuid4().hex[:8]}"})
        assert kb.status_code == 201, kb.text
        kb_id = kb.json()["id"]

        # --- ingest: JSON text (Markdown) + multipart file
        md = app.post(
            f"/v1/knowledge-bases/{kb_id}/documents/text",
            json={
                "text": HANDBOOK,
                "format": "markdown",
                "title": "Handbook",
                "external_id": "handbook",
            },
        )
        assert md.status_code == 202, md.text
        up = app.post(
            f"/v1/knowledge-bases/{kb_id}/documents",
            files={"file": ("security.txt", SECURITY, "text/plain")},
            data={"external_id": "security", "metadata": json.dumps({"team": "it"})},
        )
        assert up.status_code == 202, up.text
        handbook_id, security_id = md.json()["document"]["id"], up.json()["document"]["id"]

        for doc_id in (handbook_id, security_id):
            doc = _wait_ready(app, kb_id, doc_id)
            assert doc["status"] == "ready", doc
            assert doc["chunk_count"] >= 1

        job = app.get(f"/v1/jobs/{md.json()['job_id']}").json()
        assert job["status"] == "succeeded"

        # --- re-uploading identical content does no work
        again = app.post(
            f"/v1/knowledge-bases/{kb_id}/documents/text",
            json={"text": HANDBOOK, "format": "markdown", "external_id": "handbook"},
        )
        assert again.status_code == 200
        assert again.json()["unchanged"] is True

        # --- retrieve: the right document ranks first
        r = q.post("/v1/retrieve", json={"kb_ids": [kb_id], "query": "paid annual leave days"})
        assert r.status_code == 200, r.text
        top = r.json()["chunks"][0]
        assert top["document_id"] == handbook_id
        assert top["heading_path"][-1] == "Leave"

        # --- query (non-streaming): grounded answer with a valid citation
        a = q.post("/v1/query", json={"kb_ids": [kb_id], "query": "How many days of annual leave?"})
        assert a.status_code == 200, a.text
        answer = a.json()
        assert "[1]" in answer["answer"]
        assert answer["citations"][0]["document_id"] == handbook_id
        assert answer["invalid_citations"] == []

        # --- query (streaming): retrieval → delta* → done
        with q.stream(
            "POST",
            "/v1/query",
            json={"kb_ids": [kb_id], "query": "laptop encryption", "stream": True},
        ) as s:
            assert s.status_code == 200
            events = _sse_events(s)
        names = [n for n, _ in events]
        assert names[0] == "retrieval"
        assert names[-1] == "done"
        assert "delta" in names
        assert events[-1][1]["citations"][0]["document_id"] == security_id

        # --- delete: gone from search immediately
        d = app.delete(f"/v1/knowledge-bases/{kb_id}/documents/{security_id}")
        assert d.status_code == 202, d.text
        r = q.post(
            "/v1/retrieve", json={"kb_ids": [kb_id], "query": "laptop encryption", "top_k": 20}
        )
        assert security_id not in {c["document_id"] for c in r.json()["chunks"]}


def test_access_control(admin: httpx.Client) -> None:
    # Unauthenticated → 401.
    assert httpx.get(f"{API}/v1/knowledge-bases", timeout=10).status_code == 401

    kb_a = admin.post("/v1/knowledge-bases", json={"name": f"e2e-a-{uuid.uuid4().hex[:8]}"}).json()[
        "id"
    ]
    kb_b = admin.post("/v1/knowledge-bases", json={"name": f"e2e-b-{uuid.uuid4().hex[:8]}"}).json()[
        "id"
    ]

    restricted = _app_client(admin, allowed_kb_ids=[kb_a], scopes=["kb:read", "query"])
    headers = {"Authorization": f"Bearer {restricted}"}
    with httpx.Client(base_url=API, headers=headers, timeout=10) as app:
        assert app.get(f"/v1/knowledge-bases/{kb_a}").status_code == 200
        # A KB outside the allow-list looks exactly like a missing one.
        assert app.get(f"/v1/knowledge-bases/{kb_b}").status_code == 404
        assert [kb["id"] for kb in app.get("/v1/knowledge-bases").json()] == [kb_a]
        # Missing scope → 403.
        assert app.post("/v1/knowledge-bases", json={"name": "x"}).status_code == 403

    with httpx.Client(base_url=QUERY, headers=headers, timeout=10) as q:
        assert q.post("/v1/retrieve", json={"kb_ids": [kb_b], "query": "x"}).status_code == 404
