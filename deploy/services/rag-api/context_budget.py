"""Context budget + most-relevant-first packing (lesson 4.5).

Pure Python: no cloud calls. `count_fn` is injected so the API can pass
`client.models.count_tokens` and tests can pass a fake.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class TokenBudget:
    system: int = 1_500
    tenant_pack: int = 40_000   # stable prefix, cached (see cache_manager.py)
    chunks: int = 6_000         # retrieved evidence, packed most-relevant-first
    history: int = 2_000        # rolling summary (documind-chat, 8.5)
    answer: int = 2_000         # reserved for output

    @property
    def input_total(self) -> int:
        return self.system + self.tenant_pack + self.chunks + self.history


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def source_header(n: int, chunk: dict) -> str:
    """The [Source N] header keeps everything a citation needs to resolve.

    Read the keys DocuMind actually writes. services/ingest/indexer.py stores
    {tenant_id, text, source_uri, page_start} on every chunk - not source_file,
    not doc_id, not pages, not section. The previous version looked for all four
    of those and rendered "[Source 1] (?, p.None, )" in production: a header that
    survived because nothing asserts on the CONTEXT, only on the answer.

    Empty parts are dropped rather than printed, so a chunk with no page number
    produces "[Source 1] hr_policy_2026.md" instead of a header full of holes.
    """
    uri = chunk.get("source_uri") or chunk.get("source_file") or chunk.get("doc_id") or ""
    name = uri.rsplit("/", 1)[-1] if uri else "unknown source"
    page = chunk.get("page_start") or chunk.get("page") or chunk.get("pages")
    section = chunk.get("section") or ""
    effective = chunk.get("effective_from")           # the ledger (12.5): the date the document declares
    bits = ([name] + ([f"p.{page}"] if page else []) + ([section] if section else [])
            + ([f"effective from {effective}"] if effective else []))
    return f"[Source {n}] " + ", ".join(bits)


def pack_chunks(chunks: list[dict], budget_tokens: int,
                count_fn: Callable[[str], int] | None = None) -> tuple[str, list[dict], list[dict]]:
    """Pack reranked chunks most-relevant-first without exceeding `budget_tokens`.

    Returns (context, packed, dropped). Metadata is preserved on every packed chunk
    so `/v1/query` citations still map to doc_id / page after packing.
    """
    packed, dropped, parts, used = [], [], [], 0
    for c in chunks:
        block = f"{source_header(len(packed) + 1, c)}\n{c.get('text') or c.get('content', '')}"
        need = estimate_tokens(block)
        if used + need > budget_tokens:
            dropped.append(c)
            continue
        used += need
        packed.append(c)
        parts.append(block)
    context = "\n\n".join(parts)
    if count_fn and context:
        exact = count_fn(context)
        while exact > budget_tokens and packed:      # estimate was optimistic: trim from the tail
            dropped.insert(0, packed.pop())
            parts.pop()
            context = "\n\n".join(parts)
            exact = count_fn(context) if context else 0
    return context, packed, dropped
