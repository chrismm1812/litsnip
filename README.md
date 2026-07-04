# litsnip

Standalone macOS desktop app for cross-lingual semantic search over the
classic-literature index at `~/Projects/french-lit` (47 works in French,
German, English, Latin, ancient Greek). Successor to the deprecated
Glaze-built "Littéraire" prototype — plain Electron, no framework
dependency, fully local, zero API tokens.

Repo: https://github.com/chrismm1812/litsnip — voices and models are not
committed; after cloning run `scripts/get_voices.sh` (Piper voices) and
point `LIT_DB_PATH` at an index (or ship `index_compact/`, 40 MB).

## Adding books from the app
The **Add book** button searches Project Gutenberg (via Gutendex), shows
candidates with download counts, and one click downloads, registers,
chunks, embeds (locally, a few minutes) and hot-reloads the index — the
book is searchable immediately, no restart. Books not on Gutenberg
(Wikisource, Perseus, The Latin Library) still go through a Claude session
with the `lit-corpus` skill, which knows those sources' quirks.

## Features
- **Search in any language**, filter by corpus language / genre / author.
  BGE-M3 embeddings + bge-reranker-v2-m3, edition-portable citations
  (same logic as `french-lit/scripts/query.py`).
- **Translate** any passage into EN/FR/DE/ES/IT with a local model
  (NLLB-200-distilled-600M, first use downloads ~2.4 GB). Sentence-by-
  sentence, cached forever in `cache/translations.db` — each passage is
  translated once. Latin is natively supported; ancient Greek goes through
  the modern-Greek pathway (approximate; labeled as machine translation).
- **Read aloud (karaoke)**: tiered neural voices — **Dia-1.6B**
  (~/Projects/dia, English only, superb but ~7x slower than realtime on MPS;
  sentence-batched with per-segment progress) for English; **Piper** neural
  voices for French (siwis), German (thorsten), Greek (rapunzelina) and
  Latin (Italian paola) from `voices/`; macOS `say` only as failure
  fallback. All generated audio is cached in `cache/tts/` so each passage
  costs the generation wait exactly once. Words highlight in sync; timings
  are syllable-weight estimates scaled to true audio duration.
  `LIT_TTS_ENGINE=say` forces the fast legacy path.
- **History**: every search (query, filters, results) is persisted to
  `cache/history.db`; the History button recalls any past search with its
  stored results instantly, no re-search.
- **Results count**: explicit retrieval-k control in the UI (1–50).
- **Export video**: re-records the karaoke reading (canvas + audio) to a
  .webm you can share.

## Architecture
```
main.js          Electron main — window + spawns the Python sidecar
preload.js       contextBridge: lit.search/translate/speak/saveVideo
renderer/        vanilla HTML/CSS/JS, no build step
python/sidecar.py  NDJSON over stdio: search / translate / speak
```
The sidecar reads the LanceDB index at `~/Projects/french-lit/index`
(override with `LIT_DB_PATH`). Python interpreter: first of
`$LIT_PYTHON_BIN`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`,
`/usr/bin/python3`.

## Build profiles (lean by choice)
`LIT_PROFILE` picks the tier; leaner tiers never load, download, or show
the heavier features:

| profile | features | needs on disk |
|---|---|---|
| `search` | vector search + history only | index (119 MB lance **or** 40 MB int8 compact — auto-detected) + BGE-M3/reranker |
| `translate` | + local translation | + NLLB (~2.4 GB, HF cache, on first use) |
| `full` (default) | + read-aloud & video export | + Piper voices (300 MB in `voices/`); Dia optional |

The app folder itself stays lean; large models live in `~/.cache/huggingface`
shared machine-wide and are only fetched when a tier actually uses them.

## Voices: fast vs beautiful
Default is **Fast (Piper)** for every language — near-instant neural voices
(en lessac, fr siwis, de thorsten, el rapunzelina, la→it paola). The UI's
Voice selector offers **Dia-1.6B** (English only): a 1.6B generative speech
model — noticeably more human, but autoregressive, so ~7x slower than
realtime on MPS (~1 min per paragraph). This is the fundamental local-TTS
tradeoff: instant-and-good (Piper) vs slow-and-beautiful (Dia); instant-and-
beautiful is what cloud APIs sell you GPU time for. Every generated reading
is cached in `cache/tts/` — Dia's cost is paid once per passage, ever.

## Run / develop
```bash
cd ~/Projects/litteraire && npm start                # full
LIT_PROFILE=search npm start                         # lean tier
```
First launch loads the search models (~20 s; status shown in-app).

## Search: semantic vs thematic
Queries work best when they resemble the actual text passages — concrete
phrasings, quotes, or specific themes. Abstract philosophical concepts
("Stoic maxim", "the nature of friendship") sometimes underperform because
BGE-M3 embeds them generically, so the semantic space ranks many classical
texts equally well. Workarounds:

- **Be specific**: instead of "Maxime Stoique", search "what is virtue" or
  "accept what you cannot change". Queries that sound like passages work best.
- **Use language filters**: `language=el` (Greek), `language=la` (Latin), etc.
  constrain retrieval to the right semantic neighborhood.
- **Two-stage retrieval** (future): LLM suggests thematic passages → search
  verifies them. This would fix deep thematic queries but costs extra
  computation.

This is a known frontier in retrieval-augmented systems; semantic search on
abstract concepts alone is not yet solved. Hybrid systems (keyword + semantic)
and grounded suggestions (LLM → retrieval verification) are the state of the art.

## Known limits
- Full rebuilds of the index (`build_index.py` no-flag) must not run while
  the app is open (the sidecar holds a table snapshot; restart the app after).
- Word-highlight timing is estimated, not aligned; long pauses inside a
  passage can drift the highlight by a word or two.
- Video export is .webm (VP9); convert with ffmpeg if you need .mp4.
