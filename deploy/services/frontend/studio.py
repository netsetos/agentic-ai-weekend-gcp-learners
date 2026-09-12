"""The Media Studio tab - lesson 9.4's client half, over the API's /v1/media routes (gap: the
routes shipped on 5 September and nothing called them until Module 9 joined the lane).

Two things, and nothing that the API does not already guard: a prompt becomes an image through
/v1/media/generate - the API checks the tenant's roster, spends once, caches under DEMO_MODE,
writes the audit row and the usage row - and an answer becomes speech through voice.cached_tts
(9.2: Chirp 3 HD, cached in the TTS bucket by the SHA-256 of voice + text). This page never
holds a model client of its own; it signs the generated blob's URL the way citations.py signs a
figure's, and shows it.
"""
import os
import requests
import streamlit as st

from auth import tenant_for
from chat import RAG_API_URL, _headers
from citations import signed_url
from voice import cached_tts

IMAGE_USD, USD_INR = 0.039, 85
DEFAULT_PROMPT = ("A clean, professional grouped bar chart titled 'ACME revenue by region, FY2025 vs FY2026 "
                  "(Rs crore)': India 412 to 508, APAC 188 to 236, EMEA 96 to 91, Americas 143 to 170. "
                  "Teal palette, value labels on every bar, white background.")


def studio_page(user):
    tenant_id = tenant_for(user["email"])
    if not tenant_id:
        st.error("Your account is not a member of any DocuMind tenant.")
        st.stop()
    st.title("Media Studio")
    st.caption(f"Tenant: **{tenant_id}** - every generation is roster-checked, audited (prompt hashed) "
               f"and metered by the API; the image carries SynthID.")

    st.subheader("Generate an image")
    prompt = st.text_area("Prompt", DEFAULT_PROMPT, height=110, max_chars=2000)
    if st.button("Generate", type="primary"):
        with st.spinner("generating..."):
            r = requests.post(f"{RAG_API_URL}/v1/media/generate",
                              json={"prompt": prompt, "tenant_id": tenant_id},
                              headers=_headers(), timeout=120)
        if r.status_code != 200:
            st.error(f"media route returned {r.status_code}: {r.text[:300]}")
        else:
            body = r.json()
            bucket = body.get("bucket") or os.environ.get("MEDIA_BUCKET", f"{os.environ['GOOGLE_CLOUD_PROJECT']}-media")
            st.image(signed_url(f"gs://{bucket}/{body['blob']}"), caption=body["blob"])
            cost = 0.0 if body.get("cached") else IMAGE_USD
            st.caption(f"{'cache hit (DEMO_MODE)' if body.get('cached') else 'generated'} - "
                       f"${cost:.3f} / Rs {cost * USD_INR:.2f} - {body['blob']}")

    st.divider()
    st.subheader("Read it aloud")
    text = st.text_area("Text", "EMEA revenue fell 5.2 per cent, from 96 crore to 91 crore.", height=80, max_chars=1500)
    voice = st.selectbox("Voice", ["en-IN-Chirp3-HD-Kore", "en-IN-Chirp3-HD-Charon", "hi-IN-Chirp3-HD-Kore"])
    if st.button("Speak"):
        lang = voice[:5]
        with st.spinner("synthesising..."):
            audio = cached_tts(text, voice=voice, lang=lang)
        st.audio(audio, format="audio/ogg")
        st.caption("Chirp 3 HD, cached by SHA-256 of voice and text in the TTS bucket: the second play is free.")
