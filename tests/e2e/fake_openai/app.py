"""A tiny stand-in for the OpenAI API, used by e2e tests and CI.

Implements just what the RAG service calls:

* ``POST /v1/embeddings`` — deterministic *bag-of-words hashing* vectors. Texts that
  share words get similar vectors, so retrieval behaves meaningfully (the right
  document ranks first) without a real model.
* ``POST /v1/responses`` (streaming) — answers with the first sentence of source [1]
  and cites it, emitted as Responses-API SSE events.

Not a general mock: anything else returns 404, so unexpected calls fail loudly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import struct
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
_WORD = re.compile(r"[a-z0-9]+")


def _embed(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for word in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "little")
        vec[h % dim] += 1.0 if (h >> 63) == 0 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    body = await request.json()
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    dim = int(body.get("dimensions") or 1536)
    data = []
    for i, text in enumerate(inputs):
        vector = _embed(text, dim)
        # The SDK asks for base64 by default and decodes little-endian float32.
        embedding: Any = (
            base64.b64encode(struct.pack(f"<{dim}f", *vector)).decode()
            if body.get("encoding_format") == "base64"
            else vector
        )
        data.append({"object": "embedding", "index": i, "embedding": embedding})
    tokens = sum(len(_WORD.findall(t.lower())) for t in inputs)
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": body["model"],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        }
    )


def _answer_from(prompt: str) -> str:
    match = re.search(r'<source id="1"[^>]*>\s*(.+?)</source>', prompt, re.S)
    if not match:
        return "I don't know based on the provided documents."
    first_sentence = re.split(r"(?<=[.!?])\s+", match.group(1).strip())[0]
    return f"According to the documents, {first_sentence} [1]"


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


@app.post("/v1/responses", response_model=None)
async def responses(request: Request) -> StreamingResponse | JSONResponse:
    body = await request.json()
    prompt = "\n".join(str(m.get("content", "")) for m in body.get("input", []))
    answer = _answer_from(prompt)
    model = body.get("model", "fake-model")
    usage = {
        "input_tokens": len(prompt.split()),
        "output_tokens": len(answer.split()),
        "total_tokens": len(prompt.split()) + len(answer.split()),
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    base = {"id": "resp_fake", "object": "response", "created_at": 0, "model": model, "output": []}

    if not body.get("stream"):
        return JSONResponse({**base, "status": "completed", "usage": usage})

    def events() -> Iterator[str]:
        seq = 0
        yield _sse(
            {
                "type": "response.created",
                "sequence_number": seq,
                "response": {**base, "status": "in_progress"},
            }
        )
        for word in re.findall(r"\S+\s*", answer):
            seq += 1
            yield _sse(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": seq,
                    "item_id": "msg_fake",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": word,
                    "logprobs": [],
                }
            )
        seq += 1
        yield _sse(
            {
                "type": "response.completed",
                "sequence_number": seq,
                "response": {**base, "status": "completed", "usage": usage},
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")
