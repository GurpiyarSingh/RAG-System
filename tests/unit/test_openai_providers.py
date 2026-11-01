"""OpenAI providers exercised through the real SDK against the in-process fake server.

Routing the SDK's HTTP client into the fake app (ASGI transport) checks two things at
once: our provider code, and that the fake used by the e2e suite really speaks the
wire format the SDK expects. A real-API smoke test is marked ``live``.
"""

from __future__ import annotations

import os

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from rag_service.core.config import Settings
from rag_service.core.errors import ModelNotConfiguredError
from rag_service.providers.base import (
    ChatMessage,
    GenerationDelta,
    GenerationDone,
    GenerationRequest,
)
from rag_service.providers.embeddings.openai import OpenAIEmbeddingProvider
from rag_service.providers.llm.openai import OpenAILLMProvider
from tests.conftest import WordTokenizer
from tests.e2e.fake_openai.app import app as fake_app


def _client() -> AsyncOpenAI:
    transport = httpx.ASGITransport(app=fake_app)
    return AsyncOpenAI(
        api_key="sk-test",
        base_url="http://fake/v1",
        http_client=httpx.AsyncClient(transport=transport, base_url="http://fake/v1"),
        max_retries=0,
    )


async def test_embeddings_batch_and_preserve_order(settings: Settings) -> None:
    settings.embedding_batch_size = 2  # force several requests
    provider = OpenAIEmbeddingProvider(
        settings, client=_client(), limiter=None, tokenizer=WordTokenizer()
    )
    texts = ["leave policy", "sick days", "travel booking", ""]
    batch = await provider.embed_documents(texts)

    assert len(batch.vectors) == 4
    assert all(len(v) == settings.embedding_dimensions for v in batch.vectors)
    # Same text → same vector, and order matches input order across batches.
    again = await provider.embed_query("sick days")
    assert again.vectors[0] == pytest.approx(batch.vectors[1])


async def test_llm_stream_yields_deltas_then_usage(settings: Settings) -> None:
    provider = OpenAILLMProvider(
        settings, client=_client(), limiter=None, tokenizer=WordTokenizer()
    )
    request = GenerationRequest(
        system="Answer from sources.",
        messages=[
            ChatMessage(
                "user", '<sources>\n<source id="1">Annual leave is 20 days.</source>\n</sources>'
            )
        ],
        model="gpt-4o",
        max_output_tokens=100,
        temperature=0.1,
    )
    events = [e async for e in provider.stream(request)]

    text = "".join(e.text for e in events if isinstance(e, GenerationDelta))
    assert text == "According to the documents, Annual leave is 20 days. [1]"
    done = events[-1]
    assert isinstance(done, GenerationDone)
    assert done.output_tokens > 0
    assert done.finish_reason == "completed"


async def test_placeholder_key_fails_fast() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = OpenAIEmbeddingProvider(
        settings, client=_client(), limiter=None, tokenizer=WordTokenizer()
    )
    with pytest.raises(ModelNotConfiguredError):
        await provider.embed_query("hello")


@pytest.mark.live
async def test_live_openai_smoke() -> None:
    """Hits the real API (costs a fraction of a cent): `pytest -m live`."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key.startswith("sk-") or "your-openai-key" in key:
        pytest.skip("OPENAI_API_KEY not set")
    settings = Settings(openai_api_key=SecretStr(key))
    client = AsyncOpenAI(api_key=key)
    embed = OpenAIEmbeddingProvider(
        settings, client=client, limiter=None, tokenizer=WordTokenizer()
    )
    assert len((await embed.embed_query("hello")).vectors[0]) == settings.embedding_dimensions
