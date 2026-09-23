# Top 10 Practical RAG Interview Questions (carousel)

An eleven-page carousel: a cover plus ten scenario-based RAG interview questions, each with an
answer you can defend in the room. The questions run from easy to tough: two warm-ups, five
everyday engineering problems, three that separate senior candidates. Navy and teal palette,
a difficulty tag on every page, and a faint `@netsetos` watermark across each one.

## Files

| File | Use it for |
|---|---|
| `output/top-10-practical-rag-interview-questions.pdf` | all 11 pages, vector text; upload this as a LinkedIn document post |
| `output/png/00-cover.png` ... `output/png/10-question.png` | one 1080 x 1440 image per page, for image posts, stories or Instagram |
| `build.py` | the content (questions, answers, handle, watermark, colours) and the page template; edit this, then rebuild |
| `fonts/Lora-variable.woff2` | the serif used on every page, SIL Open Font License (`fonts/OFL.txt`) |

## The ten questions

| # | Tier | Topic | Scenario |
|---|---|---|---|
| 1 | Easy | RAG pipeline basics | A folder of policy PDFs: the minimum pipeline to build first |
| 2 | Easy | Chunking strategy | Choosing a chunk size for 5,000 internal PDFs, and what goes wrong when it is off |
| 3 | Average | Debugging the pipeline | Wrong answers: is it retrieval or generation? |
| 4 | Average | Hybrid search & reranking | Vector search misses product codes, error IDs and names |
| 5 | Average | Grounding & hallucination | The model answers from its own knowledge when the documents do not |
| 6 | Average | Index freshness | Keeping the index in sync with daily updates and deletes |
| 7 | Average | Latency & cost | An endpoint that costs too much and takes eight seconds |
| 8 | Tough | Evaluation | Evaluating a change before shipping without a big labelling budget |
| 9 | Tough | Multi-tenant isolation | Guaranteeing tenant A never sees tenant B's documents |
| 10 | Tough | Security: prompt injection | A retrieved document that says "ignore previous instructions" |

## Rebuild the deck

```bash
pip install playwright && python -m playwright install chromium
python3 build.py              # output/html, output/png and the PDF
python3 build.py --html-only  # just the HTML pages, no browser needed
python3 build.py --scale 2    # PNGs at 2160 x 2880
```

The build fails, on purpose, if an answer no longer fits above the footer band, and it names the page.

## Change something

Everything lives at the top of `build.py`:

- `HANDLE`, `FOOTER_FOLLOW`, `FOOTER_CTA`: the footer band on every question page.
- `WATERMARK`, `WATERMARK_OPACITY`: the diagonal stamp. Set the opacity to 0 to hide it.
- `COVER_BADGE`, `COVER_PILL`, `COVER_TITLE`, `COVER_SUBTITLE`: the cover. Wrap a word in `<em>` to print it in teal.
- `LEVELS`: the three difficulty tiers and their tag colours. The cover's "Q1-2 Easy" row is computed from
  each question's `level`, so reordering questions updates it.
- `QUESTIONS`: one entry per page with a `level`, a `category` (the small caps under "Question N"), the
  `question`, a one-line `lead` and the bullet `points`.
- The colours are CSS variables at the top of `CSS`: `--band` for the navy bands, `--accent` for the teal.
