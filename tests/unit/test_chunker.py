"""Structure-aware chunker: invariants (property tests) and structural behavior."""

from __future__ import annotations

import itertools

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_service.ingestion.chunkers.recursive import ChunkingConfig, RecursiveChunker
from rag_service.ingestion.types import Block, ParsedDocument
from tests.conftest import WordTokenizer

WORDS = st.text(alphabet="abcdefghij", min_size=1, max_size=8)
SENTENCE = st.lists(WORDS, min_size=1, max_size=40).map(lambda ws: " ".join(ws) + ".")
PARAGRAPH = st.lists(SENTENCE, min_size=1, max_size=12).map(" ".join)
BLOCKS = st.lists(
    st.one_of(
        PARAGRAPH.map(lambda t: Block(kind="paragraph", text=t)),
        WORDS.map(lambda t: Block(kind="heading", text=t, level=1)),
        WORDS.map(lambda t: Block(kind="heading", text=t, level=2)),
    ),
    min_size=1,
    max_size=25,
)


@settings(max_examples=150, deadline=None)
@given(blocks=BLOCKS, max_tokens=st.integers(32, 200))
def test_chunks_never_exceed_limit_and_lose_no_text(blocks: list[Block], max_tokens: int) -> None:
    tok = WordTokenizer()
    cfg = ChunkingConfig(max_tokens=max_tokens, overlap_tokens=max_tokens // 4)
    chunks = RecursiveChunker(tok, cfg).chunk(ParsedDocument(blocks=blocks))

    # Invariant 1: size limit holds for every chunk.
    assert all(c.token_count <= max_tokens for c in chunks)
    # Invariant 2: every input word appears in the output (nothing dropped).
    out_words = {w for c in chunks for w in c.text.split()}
    for block in blocks:
        if block.kind != "heading":
            assert set(block.text.split()) <= out_words
    # Invariant 3: ordinals are dense and ordered.
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_chunks_follow_heading_hierarchy(tokenizer: WordTokenizer) -> None:
    doc = ParsedDocument(
        title="Handbook",
        blocks=[
            Block(kind="heading", text="Leave", level=1),
            Block(kind="paragraph", text="Annual leave is 20 days."),
            Block(kind="heading", text="Sick leave", level=2),
            Block(kind="paragraph", text="Sick leave needs a note after 3 days."),
            Block(kind="heading", text="Travel", level=1),
            Block(kind="paragraph", text="Book through the portal."),
        ],
    )
    chunks = RecursiveChunker(tokenizer).chunk(doc)
    assert [c.heading_path for c in chunks] == [["Leave"], ["Leave", "Sick leave"], ["Travel"]]
    # The contextual header is in the embedded text, not in the displayed text.
    assert chunks[1].embed_text.startswith("Handbook > Leave > Sick leave\n\n")
    assert chunks[1].text == "Sick leave needs a note after 3 days."


def test_large_table_is_split_with_repeated_header(tokenizer: WordTokenizer) -> None:
    header = "| name | value |\n|---|---|"
    rows = "\n".join(f"| row{i} | some value here {i} |" for i in range(60))
    doc = ParsedDocument(blocks=[Block(kind="table", text=f"{header}\n{rows}")])
    chunks = RecursiveChunker(tokenizer, ChunkingConfig(max_tokens=64, overlap_tokens=0)).chunk(doc)

    assert len(chunks) > 1
    assert all(c.chunk_type == "table" for c in chunks)
    assert all(c.text.startswith(header) for c in chunks)
    assert all(c.token_count <= 64 for c in chunks)


def test_consecutive_chunks_overlap(tokenizer: WordTokenizer) -> None:
    sentences = " ".join(f"Sentence number {i} is here." for i in range(40))
    doc = ParsedDocument(blocks=[Block(kind="paragraph", text=sentences)])
    chunks = RecursiveChunker(tokenizer, ChunkingConfig(max_tokens=50, overlap_tokens=12)).chunk(
        doc
    )

    assert len(chunks) > 1
    for previous, current in itertools.pairwise(chunks):
        last_sentence = previous.text.rsplit(". ", 1)[-1]
        assert last_sentence in current.text  # trailing context carried forward
    # Sentences of one paragraph stay joined by spaces, not blank lines.
    assert all("\n\n" not in c.text for c in chunks)


def test_pages_are_tracked(tokenizer: WordTokenizer) -> None:
    doc = ParsedDocument(
        blocks=[
            Block(kind="paragraph", text="On page one.", page=1),
            Block(kind="paragraph", text="On page two.", page=2),
        ]
    )
    [chunk] = RecursiveChunker(tokenizer).chunk(doc)
    assert (chunk.page_start, chunk.page_end) == (1, 2)
