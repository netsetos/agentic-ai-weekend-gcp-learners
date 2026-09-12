"""Doc AI, chosen by residency rather than by preference."""
import io
import os

from google.cloud import documentai
from pypdf import PdfReader, PdfWriter

RESIDENCY = os.environ.get("RESIDENCY", "india")     # india | us

# Layout Parser gives you document STRUCTURE - headings, tables, reading order -
# and it runs in `us` only. Enterprise OCR runs in asia-south1 and gives you
# text plus layout boxes, no semantic structure.
#
# For a DPDP deployment that is not a trade-off you get to make on quality
# grounds: if the document carries personal data of people in India and the
# customer's contract says it stays in India, the processor that runs in `us`
# is not available to you, whatever it would have given you.
PROCESSORS = {
    "india": {"location": "asia-south1", "type": "OCR_PROCESSOR"},
    "us":    {"location": "us",          "type": "LAYOUT_PARSER_PROCESSOR"},
}

# An online (synchronous) request takes at most 15 pages, for OCR, Layout Parser and Form
# Parser alike (docs.cloud.google.com/document-ai/limits). The kit's corpus is thirteen
# Acts averaging 53 pages, so a PDF goes up in 15-page slices - lesson 4.1's slices(), the
# same limit - and its pages come back in order. Batch processing takes 500 pages but is
# asynchronous and needs an output prefix to poll; a dozen online calls inside one push
# request is simpler and stays inside the 600-second ack deadline eventarc.tf sets.
ONLINE_PAGE_LIMIT = 15


def processor_config() -> dict:
    return PROCESSORS[RESIDENCY]


def _client_and_name(project_id: str, processor_id: str):
    cfg = processor_config()
    client = documentai.DocumentProcessorServiceClient(
        client_options={"api_endpoint": f"{cfg['location']}-documentai.googleapis.com"})
    return client, f"projects/{project_id}/locations/{cfg['location']}/processors/{processor_id}"


def _page_texts(doc) -> list[str]:
    """One string per page, so the worker can put a form feed between pages and _chunk()
    can name the page a chunk starts on - the page_start a citation shows.

    OCR fills `pages`, each with a text anchor into `text`. Layout Parser fills
    `document_layout` instead: blocks with their own text and a page span. Either way the
    result is pages in order; a document with neither is one page of whatever text it has."""
    if doc.pages:
        pages = []
        for page in doc.pages:
            parts = [doc.text[int(seg.start_index):int(seg.end_index)]
                     for seg in page.layout.text_anchor.text_segments]
            pages.append("".join(parts))
        return pages
    by_page: dict[int, list[str]] = {}

    def walk(blocks):
        for block in blocks:
            page = int(block.page_span.page_start or 1)
            if block.text_block.text:
                by_page.setdefault(page, []).append(block.text_block.text)
            walk(block.text_block.blocks)
            for row in list(block.table_block.header_rows) + list(block.table_block.body_rows):
                for cell in row.cells:
                    walk(cell.blocks)
            if block.list_block.list_entries:
                for entry in block.list_block.list_entries:
                    walk(entry.blocks)

    walk(doc.document_layout.blocks)
    if by_page:
        last = max(by_page)
        return ["\n".join(by_page.get(p, [])) for p in range(1, last + 1)]
    return [doc.text]


def _process(client, name: str, content: bytes, mime_type: str) -> list[str]:
    result = client.process_document(
        request=documentai.ProcessRequest(
            name=name,
            raw_document=documentai.RawDocument(content=content, mime_type=mime_type)))
    return _page_texts(result.document)


def parse(project_id: str, processor_id: str, content: bytes,
          mime_type: str) -> tuple[str, int]:
    """Return (text with a form feed between pages, page_count)."""
    client, name = _client_and_name(project_id, processor_id)
    if mime_type == "application/pdf":
        reader = PdfReader(io.BytesIO(content))
        if len(reader.pages) > ONLINE_PAGE_LIMIT:
            pages: list[str] = []
            for first in range(0, len(reader.pages), ONLINE_PAGE_LIMIT):
                writer = PdfWriter()
                for page in reader.pages[first:first + ONLINE_PAGE_LIMIT]:
                    writer.add_page(page)
                buf = io.BytesIO()
                writer.write(buf)
                pages.extend(_process(client, name, buf.getvalue(), mime_type))
            return "\f".join(pages), len(pages)
    pages = _process(client, name, content, mime_type)
    return "\f".join(pages), len(pages)
