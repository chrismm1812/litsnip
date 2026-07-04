#!/usr/bin/env python3
"""Littéraire sidecar: search + translate + read-aloud, all local, zero tokens.

NDJSON protocol over stdin/stdout. Requests: {"id": N, "op": ..., ...}
Responses: {"id": N, "ok": true, "data": ...} or {"id": N, "ok": false, "error": ...}
Unsolicited: {"type": "status", "msg": ...} while models load.

Ops:
  search   {query, topK?, language?, genre?, author?, rerank?}
  translate {text, source, target}        -> {translation, cached}
  speak    {text, language}               -> {audio, words:[{w,start,end}], voice}
  ping     {}                             -> {device}

Models (all local): BGE-M3 + bge-reranker-v2-m3 (search, loaded at start),
facebook/nllb-200-distilled-600M (translation, lazy-loaded on first use,
~2.4 GB one-time download). TTS is macOS `say`; word timings are estimated
by syllable weight + punctuation pauses scaled to the real audio duration
(good enough for karaoke; upgradeable to whisper forced alignment later).

Translations are cached in cache/translations.db keyed on
(sha1(text), source, target) so each passage is translated at most once.
"""
import hashlib, json, math, os, re, sqlite3, subprocess, sys, tempfile, wave
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

APP_ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = Path(os.environ.get("LIT_DB_PATH", str(Path.home() / "Projects/french-lit/index")))
COMPACT_DIR = Path(os.environ.get("LIT_COMPACT_PATH",
                                  str(Path.home() / "Projects/french-lit/index_compact")))
TABLE = "french_lit"
# Build tiers: "search" (index only), "translate" (+NLLB), "full" (+voices).
# A leaner profile never loads or downloads the heavier models.
PROFILE = os.environ.get("LIT_PROFILE", "full")
TIER = {"search": 0, "translate": 1, "full": 2}.get(PROFILE, 2)
CACHE_DIR = Path(os.environ.get("LIT_CACHE_DIR", str(APP_ROOT / "cache")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

NL = chr(10)
_real_stdout = sys.stdout
sys.stdout = sys.stderr  # library chatter must not corrupt the protocol

def send(obj):
    _real_stdout.write(json.dumps(obj, ensure_ascii=False) + NL)
    _real_stdout.flush()

def status(msg):
    send({"type": "status", "msg": msg})

# ---------------------------------------------------------------- search ----

class CompactIndex:
    """Brute-force numpy search over the int8 export (index_compact/,
    produced by french-lit/scripts/quantize_index.py). ~40 MB on disk vs
    119 MB lance; exact search, equally fast at this corpus size."""
    def __init__(self, path):
        import numpy as np
        import pandas as pd
        self.np = np
        vecs = np.load(path / "vectors_int8.npy").astype(np.float32)
        vecs /= np.load(path / "scales.npy")[:, None]
        self.vecs = vecs
        self.meta = pd.read_parquet(path / "meta.parquet")

    def search(self, qvec, pool, language=None, genre=None, author=None):
        np, m = self.np, self.meta
        scores = self.vecs @ np.asarray(qvec, dtype=np.float32)
        mask = np.ones(len(m), dtype=bool)
        if language: mask &= (m["language"] == language).to_numpy()
        if genre: mask &= (m["genre"] == genre).to_numpy()
        if author: mask &= m["author"].str.contains(author, regex=False).to_numpy()
        scores = np.where(mask, scores, -np.inf)
        pool = min(pool, int(mask.sum()))
        if pool <= 0:
            return []
        top = np.argpartition(-scores, pool - 1)[:pool]
        top = top[np.argsort(-scores[top])]
        return m.iloc[top].to_dict("records")

def load_search():
    import torch
    from sentence_transformers import SentenceTransformer, CrossEncoder
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    status("Loading embedding model…")
    emb = SentenceTransformer("BAAI/bge-m3", device=device)
    status("Loading reranker…")
    rer = CrossEncoder("BAAI/bge-reranker-v2-m3", device=device)
    status("Opening index…")
    if (INDEX_DIR / f"{TABLE}.lance").exists():
        import lancedb
        tbl = lancedb.connect(str(INDEX_DIR)).open_table(TABLE)
    elif COMPACT_DIR.exists():
        status("Full index not found — using compact int8 index")
        tbl = CompactIndex(COMPACT_DIR)
    else:
        raise FileNotFoundError(f"no index at {INDEX_DIR} or {COMPACT_DIR}")
    return emb, rer, tbl, device

COLS = ["text", "author", "title", "year", "genre", "language",
        "chunk", "part", "chapter", "source_page"]

def op_search(state, req):
    emb, rer, tbl = state["emb"], state["rer"], state["tbl"]
    query = (req.get("query") or "").strip()
    if not query:
        return {"results": []}
    top_k = int(req.get("topK") or 8)
    do_rerank = req.get("rerank", True) and len(query.split()) > 1
    pool = 50 if do_rerank else top_k

    qvec = emb.encode([query], normalize_embeddings=True)[0]
    if isinstance(tbl, CompactIndex):
        rows = tbl.search(qvec, pool, language=req.get("language"),
                          genre=req.get("genre"), author=req.get("author"))
        rows = [{k: r.get(k) for k in COLS} for r in rows]
    else:
        q = tbl.search(qvec).metric("cosine").limit(pool * 3)
        where = []
        if req.get("language"): where.append(f"language = '{req['language']}'")
        if req.get("genre"): where.append(f"genre = '{req['genre']}'")
        if req.get("author"): where.append(f"author LIKE '%{req['author']}%'")
        if where: q = q.where(" AND ".join(where), prefilter=True)
        rows = q.select(COLS).to_list()[:pool]

    if do_rerank and rows:
        scores = rer.predict([(query, r["text"]) for r in rows])
        rows = [r for _, r in sorted(zip(scores, rows), key=lambda x: -x[0])]
        for s, r in zip(sorted(scores, reverse=True), rows):
            r["relevance"] = 1.0 / (1.0 + math.exp(-float(s)))
    rows = rows[:top_k]
    for r in rows:
        r.pop("_distance", None)
    log_search(req, rows)
    return {"results": rows}

# --------------------------------------------------------------- history ----

def history_db():
    db = sqlite3.connect(CACHE_DIR / "history.db")
    db.execute("""CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT DEFAULT (datetime('now', 'localtime')),
        query TEXT, params TEXT, results TEXT)""")
    return db

def log_search(req, rows):
    try:
        params = {k: req[k] for k in ("language", "genre", "author", "topK") if req.get(k)}
        db = history_db()
        db.execute("INSERT INTO searches (query, params, results) VALUES (?, ?, ?)",
                   (req.get("query", ""), json.dumps(params, ensure_ascii=False),
                    json.dumps(rows, ensure_ascii=False)))
        db.commit()
    except Exception as e:
        print(f"history log failed: {e}", file=sys.stderr)

def op_history(state, req):
    db = history_db()
    limit = int(req.get("limit") or 50)
    rows = db.execute("SELECT id, ts, query, params, results FROM searches "
                      "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"entries": [{"id": r[0], "ts": r[1], "query": r[2],
                         "params": json.loads(r[3]), "results": json.loads(r[4])}
                        for r in rows]}

def op_history_delete(state, req):
    db = history_db()
    db.execute("DELETE FROM searches WHERE id = ?", (int(req["entryId"]),))
    db.commit()
    return {"deleted": int(req["entryId"])}

def op_history_clear(state, req):
    db = history_db()
    n = db.execute("SELECT count(*) FROM searches").fetchone()[0]
    db.execute("DELETE FROM searches")
    db.commit()
    return {"cleared": n}

# ------------------------------------------------------------- translate ----

FLORES = {"fr": "fra_Latn", "de": "deu_Latn", "en": "eng_Latn",
          "la": "lat_Latn", "el": "ell_Grek", "es": "spa_Latn", "it": "ita_Latn"}

def load_nllb(state):
    if state.get("nllb"):
        return state["nllb"]
    status("Loading translation model (first use downloads it)…")
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    # 600M is fast and good for fr/de/en; swap in
    # "facebook/nllb-200-distilled-1.3B" via env for better Latin/Greek.
    name = os.environ.get("LIT_NLLB_MODEL", "facebook/nllb-200-distilled-600M")
    tok = AutoTokenizer.from_pretrained(name)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = AutoModelForSeq2SeqLM.from_pretrained(name).to(device)
    state["nllb"] = (tok, model, device)
    status("Translation model ready")
    return state["nllb"]

def cache_db():
    db = sqlite3.connect(CACHE_DIR / "translations.db")
    db.execute("CREATE TABLE IF NOT EXISTS t (key TEXT PRIMARY KEY, translation TEXT)")
    return db

def op_translate(state, req):
    text, source, target = req["text"], req["source"], req["target"]
    if source == target:
        return {"translation": text, "cached": True}
    key = hashlib.sha1(f"{source}|{target}|{text}".encode()).hexdigest()
    db = cache_db()
    hit = db.execute("SELECT translation FROM t WHERE key = ?", (key,)).fetchone()
    if hit:
        return {"translation": hit[0], "cached": True}

    tok, model, device = load_nllb(state)
    src, tgt = FLORES.get(source), FLORES.get(target)
    if not src or not tgt:
        raise ValueError(f"unsupported language pair {source}->{target}")
    import torch
    # transformers v5 no longer injects NLLB language tokens via src_lang,
    # so build the input by hand: [src_lang] tokens [eos] (verified format).
    src_id = tok.convert_tokens_to_ids(src)
    tgt_id = tok.convert_tokens_to_ids(tgt)
    # NLLB is sentence-level: translate sentence by sentence and rejoin,
    # otherwise long passages get truncated or summarized.
    sentences = re.split(r"(?<=[.!?;…»·])\s+", text.strip())
    out = []
    for s in sentences:
        if not s.strip():
            continue
        ids = [src_id] + tok(s, add_special_tokens=False, truncation=True,
                             max_length=440)["input_ids"] + [tok.eos_token_id]
        t = torch.tensor([ids]).to(device)
        gen = model.generate(input_ids=t, attention_mask=torch.ones_like(t),
                             forced_bos_token_id=tgt_id,
                             max_length=512, num_beams=4)
        out.append(tok.batch_decode(gen, skip_special_tokens=True)[0])
    translation = " ".join(out)
    # NLLB sometimes copy-fails on low-resource sources (Latin, notably):
    # the "translation" is near-identical to the input. Return it (labeled
    # machine translation anyway) but don't cache, so a better model swapped
    # in via LIT_NLLB_MODEL isn't blocked by poisoned entries.
    import difflib
    echoed = difflib.SequenceMatcher(None, text.lower(), translation.lower()).ratio() > 0.85
    if not echoed:
        db.execute("INSERT OR REPLACE INTO t VALUES (?, ?)", (key, translation))
        db.commit()
    return {"translation": translation, "cached": False, "echoed": echoed}

# ----------------------------------------------------------------- speak ----

# Engine tiers: Dia-1.6B (best, English-only, slow ~7x realtime on MPS,
# cached so each passage costs the wait once); Piper neural voices for
# fr/de/el + Latin via the Italian voice; macOS `say` as last resort.
DIA_REPO = Path.home() / "Projects/dia"
PIPER_VOICES = {"fr": "fr_FR-siwis-medium", "de": "de_DE-thorsten-medium",
                "el": "el_GR-rapunzelina-low", "la": "it_IT-paola-medium",
                "en": "en_US-lessac-medium"}
TTS_CACHE = CACHE_DIR / "tts"
TTS_CACHE.mkdir(exist_ok=True)

def split_sentences(text):
    return [s for s in re.split(r"(?<=[.!?;:…»])\s+", text.strip()) if s.strip()]

# Every engine returns a list of exactly-timed segments
# [{"text", "start", "end"}] measured from the real audio it produced, so
# the karaoke highlight can only drift *within* a sentence, never across —
# per-word estimate errors reset to zero at each segment boundary.

DIA_SR = 44100

def tts_dia(state, text, out_path):
    if "dia" not in state:
        status("Loading Dia voice model…")
        sys.path.insert(0, str(DIA_REPO))
        import torch
        from dia.model import Dia
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        state["dia"] = Dia.from_pretrained("nari-labs/Dia-1.6B",
                                           compute_dtype="float16", device=dev)
    # Dia is trained on short dialogue turns: batch sentences into ~35-word
    # groups, generate each, concatenate. Slow — status keeps the UI honest.
    groups, cur = [], []
    for s in split_sentences(text):
        cur.append(s)
        if sum(len(x.split()) for x in cur) >= 35:
            groups.append(" ".join(cur)); cur = []
    if cur: groups.append(" ".join(cur))

    import numpy as np
    parts, segs, t = [], [], 0.0
    for i, g in enumerate(groups, 1):
        status(f"Dia is reading… segment {i}/{len(groups)}")
        part = state["dia"].generate("[S1] " + g, verbose=False)
        dur = len(part) / DIA_SR
        segs.append({"text": g, "start": t, "end": t + dur})
        t += dur
        parts.append(part)
    audio = np.concatenate(parts) if len(parts) > 1 else parts[0]
    state["dia"].save_audio(str(out_path), audio)
    return segs

PIPER_GAP = 0.22  # seconds of silence inserted between sentences

def tts_piper(state, lang, text, out_path):
    from piper import PiperVoice
    key = f"piper_{lang}"
    if key not in state:
        model = APP_ROOT / "voices" / f"{PIPER_VOICES[lang]}.onnx"
        if not model.exists():
            raise FileNotFoundError(f"missing voice model {model.name}")
        state[key] = PiperVoice.load(str(model))
    voice = state[key]

    frames, segs, t, sr = [], [], 0.0, 22050
    for s in split_sentences(text):
        chunks = list(voice.synthesize(s))
        if not chunks:
            continue
        sr = chunks[0].sample_rate
        audio = b"".join(c.audio_int16_bytes for c in chunks)
        dur = len(audio) / 2 / sr
        segs.append({"text": s, "start": round(t, 3), "end": round(t + dur, 3)})
        frames.append(audio)
        frames.append(b"\x00\x00" * int(PIPER_GAP * sr))
        t += dur + PIPER_GAP
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(frames))
    return segs

# Legacy macOS fallback voices.
VOICE_PREFS = {"fr": ["Thomas", "Audrey", "Amélie", "Aurelie"],
               "de": ["Anna", "Petra", "Markus"],
               "en": ["Daniel", "Samantha", "Albert"],
               "el": ["Melina"],
               "la": ["Alice", "Luca", "Federica"]}

def installed_voices():
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    return {line.split("  ")[0].strip() for line in out.splitlines() if line.strip()}

def pick_voice(lang, have):
    for v in VOICE_PREFS.get(lang, []):
        if v in have or any(h.startswith(v) for h in have):
            return next(h for h in ([v] if v in have else sorted(have)) if h.startswith(v))
    return None  # `say` default voice

def syllable_weight(word):
    """Rough spoken-duration weight: vowel groups + a floor, plus a pause
    tax for trailing punctuation. Latin/Greek/French all approximate fine."""
    core = re.sub(r"[^\wÀ-ÿΆ-ώἀ-ῼ]", "", word)
    vowels = len(re.findall(r"[aeiouyàâéèêëîïôùûüœαεηιουωάέήίόύώἀ-ῼAEIOUY]", core, re.I))
    w = max(1.0, float(vowels)) + 0.3 * (len(core) > 7)
    if re.search(r"[.!?;:…»]$", word): w += 2.2   # sentence pause
    elif re.search(r"[,—–)]$", word): w += 0.9    # clause pause
    return w

def tts_say(state, lang, text, out_path):
    have = state.setdefault("voices", installed_voices())
    voice = pick_voice(lang, have)
    cmd = ["say", "-o", str(out_path), "--data-format=LEI16@22050"]
    if voice: cmd += ["-v", voice]
    cmd.append(text)
    subprocess.run(cmd, check=True, timeout=300)
    return voice or "system default"

def op_speak(state, req):
    text, lang = req["text"], req.get("language", "fr")
    # engine: auto (default) = fast Piper everywhere; "dia" is the explicit
    # slow-and-beautiful opt-in (English only — silently ignored otherwise)
    engine = req.get("engine") or os.environ.get("LIT_TTS_ENGINE", "auto")
    if engine == "dia" and lang != "en":
        engine = "auto"
    if engine == "auto":
        engine = "piper" if lang in PIPER_VOICES else "say"

    key = hashlib.sha1(f"{engine}|{lang}|{text}".encode()).hexdigest()
    out = TTS_CACHE / f"{key}.wav"
    meta_file = TTS_CACHE / f"{key}.json"
    voice = {"dia": "Dia 1.6B", "piper": PIPER_VOICES.get(lang, "piper"),
             "say": "macOS"}[engine]
    segs = None
    if not out.exists():
        try:
            if engine == "dia":
                segs = tts_dia(state, text, out)
            elif engine == "piper":
                segs = tts_piper(state, lang, text, out)
            else:
                voice = tts_say(state, lang, text, out)
        except Exception as e:
            status(f"{engine} failed ({e}) — falling back to macOS voice")
            voice = tts_say(state, lang, text, out)
        meta_file.write_text(json.dumps({"voice": voice, "segments": segs},
                                        ensure_ascii=False))
    elif meta_file.exists():
        meta = json.loads(meta_file.read_text())
        segs = meta.get("segments")
        voice = meta.get("voice") or voice

    with wave.open(str(out)) as w:
        duration = w.getnframes() / w.getframerate()

    # word timings: exact at segment boundaries (measured from the audio),
    # syllable-weight estimates only within a segment
    if not segs:
        segs = [{"text": text, "start": 0.0, "end": duration}]
    timings = []
    for seg in segs:
        ws = seg["text"].split()
        wts = [syllable_weight(w_) for w_ in ws]
        total = sum(wts) or 1.0
        t0, span, acc = seg["start"], seg["end"] - seg["start"], 0.0
        for w_, wt in zip(ws, wts):
            start = t0 + span * acc / total
            acc += wt
            timings.append({"w": w_, "start": round(start, 3),
                            "end": round(t0 + span * acc / total, 3)})
    return {"audio": str(out), "words": timings,
            "voice": voice or "system default", "duration": round(duration, 2)}

# ------------------------------------------------------------- add books ----

FRENCH_LIT = Path.home() / "Projects/french-lit"

def _https(url, timeout=60):
    import urllib.request, ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": "litsnip/1.0 (personal literature index; macOS)"})
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)

def op_book_search(state, req):
    """Query Gutendex (Project Gutenberg's API) for candidate volumes."""
    import urllib.parse
    url = f"https://gutendex.com/books?search={urllib.parse.quote(req.get('query', ''))}"
    if req.get("language"):
        url += f"&languages={req['language']}"
    with _https(url, 30) as r:
        data = json.load(r)
    already = {b["id"] for b in json.load(open(FRENCH_LIT / "scripts/booklist.json"))}
    out = []
    for b in data.get("results", [])[:12]:
        txt = next((v for k, v in b.get("formats", {}).items()
                    if k.startswith("text/plain")), None)
        if not txt:
            continue
        author = (b.get("authors") or [{}])[0].get("name", "Unknown")
        if "," in author:  # Gutendex gives "Surname, First"
            author = " ".join(reversed([p.strip() for p in author.split(",", 1)]))
        out.append({"id": b["id"], "title": b.get("title", "?"), "author": author,
                    "language": (b.get("languages") or ["?"])[0], "url": txt,
                    "downloads": b.get("download_count", 0),
                    "indexed": b["id"] in already})
    return {"candidates": out}

def op_book_add(state, req):
    """Download a Gutenberg text, register it in booklist.json, embed it with
    build_index.py --add, then reopen the index so results include it."""
    gid = int(req["gutenbergId"])  # NOT req["id"] — that's the protocol request id
    books = json.load(open(FRENCH_LIT / "scripts/booklist.json"))
    if any(b["id"] == gid for b in books):
        return {"added": False, "reason": "already in the index"}

    status("Downloading text from Project Gutenberg…")
    with _https(req["url"], 120) as r:
        text = r.read().decode("utf-8", errors="replace")
    n_words = len(text.split())
    if n_words < 1500:
        return {"added": False, "reason":
                f"text is only {n_words} words — likely an audiobook stub or excerpt"}
    (FRENCH_LIT / "corpus" / f"raw_{gid}.txt").write_text(text, encoding="utf-8")

    books.append({"id": gid, "title": req["title"], "author": req["author"],
                  "year": int(req.get("year") or 0), "genre": req.get("genre", "prose"),
                  "language": req["language"]})
    json.dump(books, open(FRENCH_LIT / "scripts/booklist.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    subprocess.run([sys.executable, str(FRENCH_LIT / "scripts/rename_corpus.py")],
                   check=True, capture_output=True, timeout=60)

    status(f"Indexing {n_words:,} words (embedding runs locally, a few minutes)…")
    p = subprocess.Popen([sys.executable, str(FRENCH_LIT / "scripts/build_index.py"),
                          "--add", str(gid)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in p.stdout:
        line = line.strip()
        if line:
            tail.append(line)
            status(line[:110])
    if p.wait() != 0:
        return {"added": False, "reason": "indexing failed: " + " | ".join(tail[-2:])}

    status("Reloading index…")
    if (INDEX_DIR / f"{TABLE}.lance").exists():
        import lancedb
        state["tbl"] = lancedb.connect(str(INDEX_DIR)).open_table(TABLE)
    elif COMPACT_DIR.exists():
        state["tbl"] = CompactIndex(COMPACT_DIR)
    return {"added": True, "words": n_words, "log": tail[-2:]}

# ------------------------------------------------------------------ main ----

def main():
    status("Starting…")
    state = {}
    emb, rer, tbl, device = load_search()
    state.update(emb=emb, rer=rer, tbl=tbl)
    send({"type": "ready", "device": device, "profile": PROFILE})

    def gated(min_tier, name, fn):
        def wrapper(s, r):
            if TIER < min_tier:
                raise ValueError(f"{name} is disabled in the '{PROFILE}' build profile")
            return fn(s, r)
        return wrapper

    ops = {"search": op_search, "history": op_history,
           "history_delete": op_history_delete, "history_clear": op_history_clear,
           "book_search": op_book_search, "book_add": op_book_add,
           "translate": gated(1, "translation", op_translate),
           "speak": gated(2, "read-aloud", op_speak),
           "ping": lambda s, r: {"device": device, "profile": PROFILE}}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        try:
            fn = ops.get(req.get("op"))
            if fn is None:
                raise ValueError(f"unknown op {req.get('op')!r}")
            send({"id": rid, "ok": True, "data": fn(state, req)})
        except Exception as e:
            import traceback; traceback.print_exc()
            send({"id": rid, "ok": False, "error": str(e)})

if __name__ == "__main__":
    main()
