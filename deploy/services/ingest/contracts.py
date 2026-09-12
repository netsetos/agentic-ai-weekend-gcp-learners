"""Contracts for the ingest lane. Everything crossing a queue is validated."""
import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The shape of a chunk row (12 September 2026). 1 is the ledger's shape: doc_key, current, indexed_at. 2 adds the
# chunk's own identity (chunk_hash, locator), the embedding stamp (embedding_model, embedding_version) and the
# retention field (expire_at). A backfill is explicit, never guessed from a missing field.
SCHEMA_VERSION = 2


class IngestMessage(BaseModel):
    """The object record Cloud Storage publishes on object.finalized (eventarc.tf's
    notification, payload JSON_API_V1), as we choose to see it.

    Pub/Sub hands you whatever the publisher sent. Parsing it into a model at
    the edge means a malformed message fails HERE, loudly, with a field name -
    instead of three functions later with a KeyError nobody can place.

    The record spells the type `contentType` and its size as a string of digits;
    the alias and pydantic's coercion take both. It carries no tenant: the object
    PATH does - `acme/code_on_wages_2019.pdf` - and the first segment is the
    tenant, which is why evals/upload.sh puts one prefix per tenant and why an
    object at the bucket root is poison, not a default tenant.
    """
    model_config = ConfigDict(populate_by_name=True)

    bucket: str
    name: str
    size: int = Field(ge=1)
    content_type: str = Field(alias="contentType")
    generation: str
    tenant_id: str = ""

    @field_validator("name")
    @classmethod
    def _no_traversal(cls, v: str) -> str:
        if ".." in v or v.startswith("/"):
            raise ValueError("object name escapes its prefix")
        return v

    @model_validator(mode="after")
    def _tenant_from_path(self):
        if not self.tenant_id:
            prefix, sep, rest = self.name.partition("/")
            if not sep or not prefix or not rest:
                raise ValueError("object is not under a tenant prefix")
            self.tenant_id = prefix
        return self

    @property
    def gcs_uri(self) -> str:
        return f"gs://{self.bucket}/{self.name}"


class DocumentContract(BaseModel):
    """What one ingested document looks like once it exists.

    doc_key is the IDEMPOTENCY KEY and it is derived from CONTENT, never from
    the Pub/Sub message id. The message id is stable across REDELIVERY, so it
    would deduplicate a retry - but the same PDF uploaded twice is two
    different messages with two different ids, and that is the duplicate
    users actually create. A content hash catches both.
    """
    tenant_id: str
    sha256: str
    gcs_uri: str
    pages: int
    doc_type: str = "unknown"
    # The ledger (11 September 2026): when the document says it applies from. Declared by the document, never
    # guessed by the pipeline; absent means undated. Carried onto every chunk and every citation.
    effective_from: str | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def doc_key(self) -> str:
        return f"{self.tenant_id}_{self.sha256}"

    def chunk_id(self, i: int) -> str:
        # Deterministic too: a re-run UPSERTS the same ids instead of adding a
        # second copy of every chunk beside the first.
        #
        # And TENANT-SCOPED, like doc_key. Until 7 Sept 2026 this was f"{sha256}#{i}": two
        # tenants uploading the same Act (the corpus shares seven documents between ACME and
        # Zeta or Globex) wrote the same Firestore documents, and the second ingest overwrote
        # the first tenant's chunks with its own tenant_id. ACME lost every shared document
        # to whichever tenant ingested it last, and the live eval refused eight questions
        # about documents ACME had uploaded. The colon matches the ids the notebooks mint
        # (acme:hr_policy_2026#NP-03); the same id in one tenant's index and another's is a
        # collision, never a saving.
        #
        # The id names the VERSION and the position; it is stable for citations and the golden set. The
        # chunk's identity ACROSS versions is its chunk_hash and its locator (12 September 2026), two
        # fields on the row - a decision recorded in deploy/INDEXING.md.
        return f"{self.tenant_id}:{self.sha256}#{i}"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_WS = re.compile(r"\s+")


def chunk_hash(text: str) -> str:
    """The chunk's identity across versions: the hash of its text with the whitespace collapsed. A re-issued
    document keeps every chunk whose hash it keeps - the carry-over in indexer.py copies their vectors and
    embeds only the rest. Whitespace is collapsed because a re-wrapped paragraph is the same paragraph."""
    return hashlib.sha256(_WS.sub(" ", text).strip().encode("utf-8")).hexdigest()


def is_stale(event_generation, ledger_generation) -> bool:
    """The generation guard. Cloud Storage numbers every version of an object; the ledger records the generation
    it indexed. An event carrying an OLDER generation than the ledger's is a late redelivery (push delivery is
    at-least-once and not in order), and acting on it would make an old version current again. A generation
    the ledger has not seen, or a ledger with none, is never stale."""
    try:
        return int(str(event_generation)) < int(str(ledger_generation))
    except (TypeError, ValueError):
        return False


# A document declares its own effective date: `effective_from: 2026-10-01` (or `Effective from: ...`) in its first
# lines - Markdown front matter or a header line - or `_effective_2026-10-01` in its object name for a PDF nobody
# can edit. Two documents that disagree are then a question of dates, not of which one the reranker liked.
_EFFECTIVE_TEXT = re.compile(r"effective[ _-]?(?:from|date)?\s*[:=]\s*(\d{4}-\d{2}-\d{2})", re.I)
_EFFECTIVE_NAME = re.compile(r"_effective_(\d{4}-\d{2}-\d{2})\.")


def effective_from_of(name: str, text: str | None) -> str | None:
    """The date a document declares, from its object name first, then its first lines; None when undated."""
    m = _EFFECTIVE_NAME.search(name or "")
    if m:
        return m.group(1)
    if text:
        m = _EFFECTIVE_TEXT.search(text[:3000])
        if m:
            return m.group(1)
    return None
