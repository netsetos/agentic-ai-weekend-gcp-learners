"""The DocuMind answer contract. One definition, imported everywhere. Gap G1.

Until 2026-09-05 there were THREE shapes of "a cited answer" in this course:

    3.2      Citation(chunk_id: int, quote)                      + RAGAnswer
    4.2      Citation(source_id: int, chunk_id: str, relevance)  + RAGResponse(..., needs_more_context)
    rag-api  Citation(chunk_id: str, source_uri, page, quote, score) + RAGAnswer(+model, tokens, latency)

and the plan's gate for Module 3 - "RAGAnswer is byte-identical in 3.2, 4.2 and rag-api" - was
not met. Worse, 9.6's multimodal fields (kind, media_url, start, end) had nowhere to live: the
shared tool projected them and FastAPI stripped them at the response model.

THREE THINGS, NOT ONE, because they are three different moments:

    ModelDraft   what Gemini is ASKED FOR. It cites by [Source N] index, because that is all it
                 can see. It never knows a chunk id, a page, or a score.
    Citation     what a CALLER receives. Resolved from the draft against the chunks the model
                 actually saw, so it carries the id, the source, the page, the score - and,
                 for a figure or a video segment, where to look.
    RAGAnswer    the contract every module hands to the next: answer, citations, confidence,
                 answerable. Nothing about tokens or latency - that is a transport envelope,
                 and rag-api adds it in its own RAGResponse subclass.

3.2 and 4.2 reproduce the three classes below VERBATIM (their notebooks say so), which is what
"byte-identical" means in practice: the text is the same, and the tests in 3.2 import it.
"""
# No `from __future__ import annotations`, on purpose. 3.2 and 4.2 exec these classes as
# a notebook cell, where a deferred `List` never resolves and pydantic refuses to build
# the model. Plain annotations work everywhere the text is pasted, which is the point.
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """One passage a caller can open. Text by default; a figure, a table or a video segment
    when the chunk is one - the four extra fields are optional and additive, so every citation
    written before Module 9 comes through unchanged."""

    chunk_id: str
    source_uri: str
    page: Optional[int] = None
    quote: str = Field(max_length=500)
    score: float = Field(ge=0, le=1)
    kind: Literal["text", "figure", "table", "segment"] = "text"
    media_url: Optional[str] = None            # a signed URL for figure / table / segment
    start: Optional[float] = None              # seconds, for a video or audio segment
    end: Optional[float] = None


class RAGAnswer(BaseModel):
    """The contract. Modules 3 to 13 all pass this shape along."""

    answer: str
    citations: List[Citation]
    confidence: Literal["high", "medium", "low"]
    answerable: bool


class DraftCitation(BaseModel):
    """What the model cites: the [Source N] number it saw, and the words it is relying on."""

    source: int = Field(ge=1, description="1-based [Source N] in the context")
    quote: str = Field(max_length=200, description="Exact words from that source")


class ModelDraft(BaseModel):
    """What the model is asked for. Use this as response_schema; resolve() turns it into a
    RAGAnswer. Asking the model for chunk ids or scores directly invites it to invent them."""

    answer: str
    citations: List[DraftCitation]
    confidence: Literal["high", "medium", "low"]
    answerable: bool


def resolve(draft: ModelDraft, packed: List[dict]) -> RAGAnswer:
    """Turn the model's [Source N] citations into Citations, against the chunks it SAW.

    `packed` must be the list the context was built from - after the token budget dropped
    anything - not the list retrieval returned. Indexing into the pre-budget list shifts every
    citation after a dropped chunk by one, silently, which is exactly the bug rag-api had.

    An out-of-range index is dropped, not raised: the model's answer is still worth returning,
    and a citation to a source that was not in the context is not a citation.
    """
    cits: List[Citation] = []
    for d in draft.citations:
        if not 1 <= d.source <= len(packed):
            continue
        c = packed[d.source - 1]
        cits.append(Citation(
            chunk_id=str(c.get("chunk_id") or c.get("id") or ""),
            source_uri=c.get("source_uri", ""),
            page=c.get("page_start") or c.get("page"),
            quote=(d.quote or c.get("text", ""))[:500],
            score=float(min(1.0, max(0.0, c.get("rerank_score") or c.get("score") or 0.0))),
            kind=c.get("kind", "text"),
            media_url=c.get("media_url"),
            start=c.get("start"),
            end=c.get("end"),
        ))
    return RAGAnswer(answer=draft.answer, citations=cits,
                     confidence=draft.confidence, answerable=draft.answerable)
