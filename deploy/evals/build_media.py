#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the corpus's media (Module 9): the figures the text has referenced since Module 4 and never
had, one real Act page, and - on request - the town hall video.

    python evals/build_media.py            # the three PNGs under corpus/acme/ (skips ones that exist)
    python evals/build_media.py --force    # re-render them
    python evals/build_media.py --video    # + corpus/acme/townhall_2026_q1.mp4: Text-to-Speech + ffmpeg

    make media                             # the same, from deploy/
    make media MEDIA_ARGS=--video

What is real and what is not, exactly as evals/README.md says of the text:

  annual_report_2026_fig3.png       DRAWN from the report's own AR-02 table (matplotlib). The report has
                                    said "[Figure 3, p.12] Revenue by region - grouped bar chart" since
                                    build_corpus.py wrote it; this is that figure. Every number on it is
                                    in the text beside it, so a golden row can ask for the figure and
                                    check the answer against the table (jn-06, mm-01).
  inv_2026_0412.png                 the Hinglish invoice RENDERED as a page image (the English half of
                                    each bilingual label - the built-in font has no Devanagari). 9.1
                                    and 9.3 OCR it; the ground truth is inv_2026_0412.md, in the corpus.
  payment_of_bonus_act_1965_p30.png page 30 of the REAL Payment of Bonus Act 1965 (the Fourth Schedule's
                                    set-on / set-off table), rendered by pypdfium2 from the PDF
                                    fetch_real.py downloaded. A real table on a real page.
  townhall_2026_q1.mp4  (--video)   SYNTHETIC: two Chirp 3 HD voices read townhall_2026_q1.md over four
                                    drawn slides, muxed by ffmpeg (apt-get in Cloud Shell, or the static
                                    build imageio-ffmpeg installs). The first slide says so. gitignored -
                                    the owner builds it, or records a real product walkthrough of the
                                    same length under the same name.

The PNGs are deterministic and committed beside the text. They are skipped when present because a
re-render on another machine can differ by a few bytes, and a different byte string is a different
document to the ingest worker (its claim key is the content hash): the corpus would hold the figure
twice. `--force` is for changing the drawing on purpose.

Pins (PyPI, 9 Sept 2026): matplotlib 3.11.1, Pillow 12.3.0, pypdfium2 5.13.0, google-cloud-texttospeech 2.37.0,
imageio-ffmpeg 0.6.0 (optional: a static ffmpeg).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")
ACME = os.path.join(CORPUS, "acme")
TEAL, TEAL_SOFT, NAVY, RED, INK = "#0d9488", "#99c9c2", "#0f1729", "#b91c1c", "#1f2937"


# ----------------------------------------------------------------------------- fonts (Pillow)
def font(size: int):
    """Pillow's built-in scalable font (10.1+). No file on disk, no Devanagari: see the invoice note."""
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                      # Pillow < 10.1: one size only
        return ImageFont.load_default()


# ----------------------------------------------------------------------------- Figure 3
def read_region_table(md_path: str) -> list[tuple[str, int, int, str]]:
    """The AR-02 table, from the Markdown the text pipeline indexes - the figure is drawn from the
    same numbers the text chunk carries, which is the whole point."""
    rows = []
    for line in open(md_path, encoding="utf-8"):
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"-?\d[\d,]*", cells[1].replace(",", "")) and cells[0] != "Total":
            rows.append((cells[0], int(cells[1].replace(",", "")), int(cells[2].replace(",", "")), cells[3]))
    if len(rows) < 3:
        raise SystemExit(f"no region table in {md_path} - run build_corpus.py first")
    return rows


def render_fig3(out: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = read_region_table(os.path.join(ACME, "annual_report_2026.md"))
    regions = [r[0] for r in rows]
    fy25, fy26, growth = [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows]
    x = list(range(len(regions)))
    w = 0.36
    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    b1 = ax.bar([i - w / 2 for i in x], fy25, w, color=TEAL_SOFT, label="FY2025")
    b2 = ax.bar([i + w / 2 for i in x], fy26, w, color=TEAL, label="FY2026")
    for bars in (b1, b2):
        for b in bars:
            ax.annotate(f"{int(b.get_height()):,}", (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=11, color=INK, xytext=(0, 3), textcoords="offset points")
    for i, g in enumerate(growth):
        ax.annotate(g if g.startswith("-") else "+" + g, (i + w / 2, fy26[i]), ha="center", va="bottom",
                    fontsize=11, fontweight="bold", color=RED if g.startswith("-") else TEAL,
                    xytext=(0, 18), textcoords="offset points")
    ax.set_xticks(x, regions, fontsize=12)
    ax.set_ylabel("Revenue (Rs crore)", fontsize=12)
    ax.set_ylim(0, max(fy26 + fy25) * 1.22)
    ax.set_title("Figure 3. Revenue by region, FY2025 vs FY2026 (Rs crore)", fontsize=15, color=NAVY, pad=14)
    ax.legend(frameon=False, fontsize=11, loc="upper right")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color="#e5e7eb", linewidth=0.8)
    ax.set_axisbelow(True)
    total25, total26 = sum(fy25), sum(fy26)
    fig.text(0.5, 0.015, f"ACME Manufacturing, Annual Report FY2026, p.12. Total {total25:,} to {total26:,} crore "
                         f"(+{(total26 / total25 - 1) * 100:.1f}%). Synthetic figures.",
             ha="center", fontsize=10, color="#4b5563")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out, dpi=100, metadata={"Software": None})
    plt.close(fig)
    from PIL import Image
    Image.open(out).convert("RGB").save(out, optimize=True)      # RGB like the other two: one shape for DLP and Vision


# ----------------------------------------------------------------------------- the invoice as a page
DEVANAGARI = re.compile(r"\s*/\s*[ऀ-ॿ][^:|]*")


def english_half(line: str) -> str:
    """'Date / दिनांक: 12 April 2026' -> 'Date: 12 April 2026'. The built-in font cannot draw Devanagari;
    the bilingual source stays in inv_2026_0412.md, which is what the OCR is checked against."""
    line = DEVANAGARI.sub("", line)
    return re.sub(r"[ऀ-ॿ]+", "", line).replace("**", "").replace("—", "-").strip()   # the built-in font has no em dash either


def render_invoice(out: str) -> None:
    from PIL import Image, ImageDraw

    lines = [english_half(l.rstrip()) for l in open(os.path.join(ACME, "inv_2026_0412.md"), encoding="utf-8")]
    W, H, M = 1240, 1754, 96
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    y = M
    big, body, small = font(56), font(30), font(22)
    d.text((M, y), lines[0].lstrip("# ").strip() or "INVOICE", fill=NAVY, font=big)
    y += 90
    d.line((M, y, W - M, y), fill=TEAL, width=4)
    y += 34
    cols = (M, M + 620, M + 740, M + 900)
    for line in lines[1:]:
        if not line:
            y += 18
            continue
        if line.startswith("|---"):
            d.line((M, y + 6, W - M, y + 6), fill="#9ca3af", width=2)
            y += 20
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            for cx, cell in zip(cols, cells):
                d.text((cx, y), cell, fill=INK, font=body)
            y += 46
            continue
        f = small if line.lower().startswith("synthetic") else body
        d.text((M, y), line, fill="#6b7280" if f is small else INK, font=f)
        y += 40 if f is small else 46
    d.text((M, H - M), "Rendered from inv_2026_0412.md by evals/build_media.py - synthetic PAN, GSTIN and mobile.",
           fill="#6b7280", font=small)
    img.save(out, optimize=True)


# ----------------------------------------------------------------------------- a real page
def render_act_page(out: str, pdf: str = "payment_of_bonus_act_1965.pdf", page: int = 30, scale: float = 1.6) -> bool:
    src = os.path.join(ACME, pdf)
    if not os.path.isfile(src):
        print(f"  skip {os.path.basename(out)}: {pdf} is not in corpus/acme (run fetch_real.py first)")
        return False
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(src)
    if page > len(doc):
        raise SystemExit(f"{pdf} has {len(doc)} pages, no page {page}")
    doc[page - 1].render(scale=scale).to_pil().convert("RGB").save(out, optimize=True)
    return True


# ----------------------------------------------------------------------------- the town hall video
SPEAKER = re.compile(r"^([A-Z][a-z]+) \((CEO|CFO)\): (.*)$")


def utterances(md_path: str) -> list[tuple[str, str]]:
    """[(speaker, text)] from the transcript: a 'Name (ROLE): ' line starts one, its continuation
    lines join it, a blank line ends it."""
    out, cur = [], None
    for raw in open(md_path, encoding="utf-8"):
        line = raw.strip()
        m = SPEAKER.match(line)
        if m:
            cur = [m.group(1), m.group(3)]
            out.append(cur)
        elif line and cur is not None and not line.startswith("#"):
            cur[1] += " " + line
        elif not line:
            cur = None
    return [(s, t) for s, t in out]


def pick_voices(client, language: str = "en-IN") -> dict[str, str]:
    """Two Chirp 3 HD voices, by NAME from the API - never hardcoded: the list moves (9.2)."""
    names = [v.name for v in client.list_voices(language_code=language).voices if "Chirp3-HD" in v.name]
    if len(names) < 2:
        raise SystemExit(f"fewer than two Chirp3-HD voices for {language}: {names}")
    first = next((n for n in names if n.endswith(("Kore", "Aoede", "Leda"))), names[0])
    second = next((n for n in names if n.endswith(("Charon", "Puck", "Orus")) and n != first),
                  next(n for n in names if n != first))
    return {"Meera": first, "Arjun": second}


def synthesize(client, text: str, voice: str, language: str = "en-IN", rate: int = 24000) -> bytes:
    from google.cloud import texttospeech as tts
    resp = client.synthesize_speech(
        input=tts.SynthesisInput(text=text),
        voice=tts.VoiceSelectionParams(language_code=language, name=voice),
        audio_config=tts.AudioConfig(audio_encoding=tts.AudioEncoding.LINEAR16, sample_rate_hertz=rate))
    data = resp.audio_content
    return data[44:] if data[:4] == b"RIFF" else data          # PCM16 mono, header off


def slide(title: str, lines: list[str], footer: str, w: int = 1280, h: int = 720):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), NAVY)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 18, h), fill=TEAL)
    d.text((72, 60), title, fill="white", font=font(46))
    y = 150
    for line in lines:
        d.text((72, y), line, fill="#e6edf3", font=font(30))
        y += 50
    d.text((72, h - 70), footer, fill="#99c9c2", font=font(22))
    return img


def ffmpeg_exe() -> str:
    """ffmpeg on PATH, else the static build imageio-ffmpeg ships, else the two ways to get one. Cloud
    Shell does NOT carry ffmpeg (the first --video run, 9 September 2026): apt-get installs it for the
    VM's life, pip installs a static binary into the home directory, which persists."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise SystemExit("ffmpeg is not on PATH. Cloud Shell: `sudo apt-get install -y ffmpeg` (per VM) or "
                         "`pip install --user imageio-ffmpeg==0.6.0` (a static build, persists in $HOME); then re-run.")


def build_video(out: str, rate: int = 24000, gap_s: float = 0.7) -> None:
    ffmpeg = ffmpeg_exe()
    from google.cloud import texttospeech as tts

    parts = utterances(os.path.join(ACME, "townhall_2026_q1.md"))
    rows = read_region_table(os.path.join(ACME, "annual_report_2026.md"))
    client = tts.TextToSpeechClient()
    voices = pick_voices(client)
    print(f"  voices: {voices}")

    work = tempfile.mkdtemp(prefix="townhall-")
    pcm, segments, t = bytearray(), [], 0.0
    silence = b"\x00" * int(rate * gap_s) * 2
    for i, (who, text) in enumerate(parts):
        audio = synthesize(client, text, voices[who], rate=rate)
        dur = len(audio) / 2 / rate
        segments.append({"i": i, "speaker": who, "start": round(t, 2), "end": round(t + dur, 2), "text": text})
        pcm += audio + silence
        t += dur + gap_s
        print(f"  {who:5} {segments[-1]['start']:6.1f}-{segments[-1]['end']:6.1f}s  {text[:60]}...")
    wav_path = os.path.join(work, "narration.wav")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(pcm))

    table = [f"{r[0]:<16} {r[1]:>5}  ->  {r[2]:>5}   ({r[3]})" for r in rows]
    slides = [
        slide("ACME Manufacturing - FY2026 Town Hall",
              ["SYNTHETIC VIDEO. Two synthetic voices reading townhall_2026_q1.md.",
               "Generated by evals/build_media.py (Text-to-Speech API + ffmpeg).", "No real person appears or speaks."],
              "DocuMind corpus - lesson 9.4 / 9.6"),
        slide("Revenue by region, FY2025 to FY2026 (Rs crore)", table + ["", "Annual Report FY2026, AR-02 / Figure 3"],
              "synthetic figures"),
        slide("People and capital", ["Headcount 4,180 (from 3,742)", "Attrition 11.4% (from 14.9%)", "",
                                     "Capex Rs 78 crore: Hyderabad plant 31, cloud and data platform 12"],
              "Annual Report FY2026, AR-05 / AR-09"),
        slide("Questions", ["Open on the portal until Friday.", "Recording and transcript in DocuMind this afternoon."],
              "synthetic video - no real person"),
    ]
    slide_of = [0, 1, 2, 2, 3][:len(parts)] + [3] * max(0, len(parts) - 5)
    durations = [0.0] * len(slides)
    for seg, s in zip(segments, slide_of):
        durations[s] += (seg["end"] - seg["start"]) + gap_s
    concat = os.path.join(work, "slides.txt")
    with open(concat, "w", encoding="utf-8") as f:
        for i, (img, dur) in enumerate(zip(slides, durations)):
            p = os.path.join(work, f"slide{i}.png").replace(os.sep, "/")
            img.save(p)
            f.write(f"file '{p}'\nduration {dur:.2f}\n")
        f.write(f"file '{p}'\n")                       # the concat demuxer needs the last frame twice
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat, "-i", wav_path,
           "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "12",
           "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    with open(out[:-4] + ".segments.json", "w", encoding="utf-8") as f:
        json.dump({"voices": voices, "rate": rate, "segments": segments}, f, indent=1)
    shutil.rmtree(work, ignore_errors=True)
    print(f"  {os.path.relpath(out, HERE)}: {os.path.getsize(out) / 1e6:.1f} MB, {t:.0f} s, {len(segments)} utterances; "
          f"ground truth beside it in townhall_2026_q1.segments.json")


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true", help="re-render PNGs that exist")
    ap.add_argument("--video", action="store_true", help="also synthesise corpus/acme/townhall_2026_q1.mp4")
    a = ap.parse_args()
    if not os.path.isdir(ACME):
        sys.exit("corpus/acme missing - run build_corpus.py first")
    jobs = [("annual_report_2026_fig3.png", render_fig3),
            ("inv_2026_0412.png", render_invoice),
            ("payment_of_bonus_act_1965_p30.png", render_act_page)]
    for name, fn in jobs:
        out = os.path.join(ACME, name)
        if os.path.isfile(out) and not a.force:
            print(f"  keep {name} ({os.path.getsize(out) // 1024} KB) - exists; --force re-renders")
            continue
        if fn(out) is False:
            continue
        print(f"  wrote {name} ({os.path.getsize(out) // 1024} KB)")
    if a.video:
        build_video(os.path.join(ACME, "townhall_2026_q1.mp4"))
    else:
        print("  (no --video: the town hall MP4 is built with --video, in a shell that has ffmpeg and the "
              "Text-to-Speech API; or drop a recording in as corpus/acme/townhall_2026_q1.mp4)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
