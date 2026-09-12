"""Citations on the screen - text, figures and video segments. Lesson 9.6, gap G7.

A citation is what the reader can OPEN. For a text chunk that is the page of the source; for a
figure it is the figure itself, inline, captioned by page; for a video segment it is the video,
started at the second the segment begins. The four extra fields (kind, media_url, start, end)
are optional and default to text, so a citation written before Module 9 renders exactly as it
did before Module 9 - and the pill in the prose says [Fig 3, p.12] or [Clip 2, 03:20] instead
of a bare [3], which is the M09 gate sentence made visible.
"""
import os, re
import datetime
import google.auth
import google.auth.credentials
import google.auth.transport.requests
import streamlit as st
from google.cloud import storage

_storage = storage.Client()
SIGNED_URL_EXPIRY = datetime.timedelta(minutes=15)
_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

# SIGNING ON CLOUD RUN. The service's credential is a token from the metadata server, not a key, and
# generate_signed_url will not sign with it: "you need a private key to sign credentials" (the first
# live upload URL, 9 September 2026), with iam.serviceAccountTokenCreator granted and unused. Handed
# the service's email and access token instead, the library signs through the IAM signBlob API - the
# path that grant exists for. A credential that can sign itself (a key file, an impersonated
# credential in a notebook) needs nothing. services/rag-api/media.py carries the same twelve lines: the frontend image
# has no shared/ to import them from, and the gate holds the two copies to one shape.
def _signing_kwargs() -> dict:
    creds, _ = google.auth.default()
    if isinstance(creds, google.auth.credentials.Signing):
        return {}
    creds.refresh(google.auth.transport.requests.Request())
    return {"service_account_email": creds.service_account_email, "access_token": creds.token}


def signed_url(gcs_uri, page=None):
    # gcs_uri: gs://bucket/path -> V4 signed URL, signed through IAM as documind-ui-sa (sa.tf:
    # ui_self_impersonate). The first figure the lane cited would have thrown here without it.
    _, _, rest = gcs_uri.partition("gs://")
    bucket_name, _, blob_name = rest.partition("/")
    blob = _storage.bucket(bucket_name).blob(blob_name)
    url = blob.generate_signed_url(version="v4", expiration=SIGNED_URL_EXPIRY, method="GET", **_signing_kwargs())
    return f"{url}#page={page}" if page else url


def _mmss(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60:02d}:{s % 60:02d}"


def _label(n: int, src: dict | None) -> str:
    """What the pill says. Text: [3]. Figure: [Fig 3, p.12]. Segment: [Clip 3, 03:20]."""
    if not src:
        return f"[{n}]"
    kind = src.get("kind", "text")
    if kind in ("figure", "table"):
        page = src.get("page_start")
        return f"[{'Fig' if kind == 'figure' else 'Table'} {n}{f', p.{page}' if page else ''}]"
    if kind == "segment":
        return f"[Clip {n}, {_mmss(src.get('start'))}]"
    return f"[{n}]"


def render_with_citations(answer, sources):
    # sources: list of {"text", "source_uri", "page_start", "kind", "media_url", "start", "end"};
    # index N -> sources[N-1]. Only the first three are guaranteed; the rest default.
    def _pill(match):
        nums = [int(n) for n in match.group(1).replace(" ", "").split(",")]
        spans = []
        for n in nums:
            src = sources[n - 1] if 0 < n <= len(sources) else None
            tip = (src["text"][:120] + "...") if src else "unknown source"
            tip = tip.replace('"', '&quot;')
            spans.append(
                f'<span title="{tip}" style="background:#ccfbf1;color:#065f46;'
                f'border-radius:6px;padding:1px 7px;margin:0 2px;font-size:12px;'
                f'font-weight:600;cursor:help;">{_label(n, src)}</span>')
        return "".join(spans)
    html = _CITE.sub(_pill, answer)
    st.markdown(html, unsafe_allow_html=True)

    with st.expander(f"📚 Sources ({len(sources)})"):
        for i, src in enumerate(sources, 1):
            page = src.get("page_start")
            kind = src.get("kind", "text")
            st.markdown(f"**{_label(i, src)}** {src['text'][:200]}...")
            if src.get("effective_from"):
                st.caption(f"effective from {src['effective_from']}")     # the ledger (12.5): the document's date
            try:
                if kind in ("figure", "table") and src.get("media_url"):
                    # The figure itself, inline - retrieved BY its caption, shown TO the human.
                    st.image(signed_url(src["media_url"]),
                             caption=f"{_label(i, src)} · {src['source_uri'].rsplit('/', 1)[-1]}")
                elif kind == "segment" and src.get("media_url"):
                    start = int(float(src.get("start") or 0))
                    st.video(signed_url(src["media_url"]), start_time=start)
                    st.caption(f"{_mmss(src.get('start'))} – {_mmss(src.get('end'))} of "
                               f"{src['source_uri'].rsplit('/', 1)[-1]}")
                else:
                    url = signed_url(src["source_uri"], page=page)
                    label = f"Jump to page {page}" if page else "Open source"
                    st.link_button(label, url)
            except Exception as e:
                st.caption(f"(signed URL unavailable: {e})")
