import os, hashlib
import streamlit as st
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech as cs
from google.api_core.client_options import ClientOptions
from google.cloud import texttospeech as tts, storage

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
REGION = os.environ.get("SPEECH_REGION", "asia-south1")
TTS_CACHE_BUCKET = storage.Client().bucket(os.environ["TTS_CACHE_BUCKET"])

_speech = SpeechClient(client_options=ClientOptions(
    api_endpoint=f"{REGION}-speech.googleapis.com"))
_tts = tts.TextToSpeechClient()

def transcribe(audio_bytes, language_codes=("hi-IN", "en-IN"), model="chirp_3"):
    cfg = cs.RecognitionConfig(
        auto_decoding_config=cs.AutoDetectDecodingConfig(),
        language_codes=list(language_codes),
        model=model,
        features=cs.RecognitionFeatures(enable_automatic_punctuation=True))
    req = cs.RecognizeRequest(
        recognizer=f"projects/{PROJECT}/locations/{REGION}/recognizers/_",
        config=cfg, content=audio_bytes)
    resp = None
    try: resp = _speech.recognize(request=req)
    except Exception:
        for m in ("chirp_2", "long"):
            cfg.model = m; req.config = cfg
            try: resp = _speech.recognize(request=req); break
            except Exception: continue
    if resp is None:
        return ""   # all STT attempts failed; degrade gracefully
    return " ".join(r.alternatives[0].transcript for r in resp.results
                    if r.alternatives).strip()

def cached_tts(text, voice="en-IN-Chirp3-HD-Kore", lang="en-IN", rate=1.0):
    cache_key = hashlib.sha256(f"{voice}|{rate}|ogg|{text}".encode()).hexdigest()
    blob = TTS_CACHE_BUCKET.blob(f"tts/{cache_key}.ogg")
    if blob.exists():
        return blob.download_as_bytes()
    # Cache miss - synthesize
    cfg = tts.StreamingSynthesizeConfig(
        voice=tts.VoiceSelectionParams(language_code=lang, name=voice),
        streaming_audio_config=tts.StreamingAudioConfig(
            audio_encoding=tts.AudioEncoding.OGG_OPUS, speaking_rate=rate))
    def gen():
        yield tts.StreamingSynthesizeRequest(streaming_config=cfg)
        yield tts.StreamingSynthesizeRequest(
            input=tts.StreamingSynthesisInput(text=text))
    audio = b"".join(r.audio_content for r in _tts.streaming_synthesize(gen()))
    blob.upload_from_string(audio, content_type="audio/ogg")
    return audio
