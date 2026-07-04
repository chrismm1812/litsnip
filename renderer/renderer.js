/* Littéraire renderer: search UI, local translation, karaoke read-aloud,
   and video export. All state lives here; heavy work is in the sidecar. */

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
let ready = false;

// Build tiers: search=0, translate=1, full=2. Elements tagged data-tier="N"
// only exist at tier >= N; per-hit buttons check TIER too.
const TIER = { search: 0, translate: 1, full: 2 }[window.lit.profile] ?? 2;
document.querySelectorAll("[data-tier]").forEach((el) => {
  if (TIER < Number(el.dataset.tier)) el.remove();
});

window.lit.onEvent((msg) => {
  if (msg.type === "status") statusEl.textContent = msg.msg;
  if (msg.type === "ready") { ready = true; statusEl.textContent = `Ready (models on ${msg.device})`; }
  if (msg.type === "died") { statusEl.textContent = "Search engine stopped — restart the app."; statusEl.className = "error"; }
});

/* ---------------- citation (port of scripts/query.py logic) ------------- */

const SMALL_WORDS = new Set(["de","du","des","la","le","les","et","un","une","à",
                             "en","au","aux","d","l","sur","dans","ou","se","sa"]);
const NUMBERED = ["Chapitre","Chapter","Kapitel","Caput","ACT","BOOK","EPISODE","Epistula",
                  "Erster","Zweiter","Dritter","Vierter","Fünfter","Sechster",
                  "First","Second","Third","Fourth","Fifth","Sixth"];
const CHAPTER_WORD = { fr: "Chapitre", de: "Kapitel", en: "Chapter", la: "Caput" };
const ROMAN = /^(?=[IVXLC])C{0,3}(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\.?$/;

function smartTitleCase(s) {
  return s.split(" ").map((w, i) => {
    if (ROMAN.test(w) || (w.includes(".") && w.slice(0, -1).includes(".") && w === w.toUpperCase())) return w;
    return w.split(/([-'’])/).map((piece, j) => {
      if (piece === "-" || piece === "'" || piece === "’") return piece;
      const lower = piece.toLowerCase();
      if (i > 0 && j === 0 && SMALL_WORDS.has(lower)) return lower;
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    }).join("");
  }).join(" ");
}

function localizeDivision(chapter, h) {
  if (!chapter.startsWith("Chapitre ")) return chapter;
  const lang = h.language || "fr";
  if (lang === "fr") return chapter;
  const num = chapter.slice("Chapitre ".length);
  if (h.genre === "poetry") return num;
  return `${CHAPTER_WORD[lang] || "Chapter"} ${num}`;
}

function citation(h) {
  const base = `${h.author} — ${h.title} (${h.year})`;
  const chapter = localizeDivision(h.chapter || "", h);
  const part = h.part || "";
  let ref;
  if (!chapter) ref = `${base}, chunk ${h.chunk}`;
  else if (NUMBERED.some((p) => chapter.startsWith(p)) || ROMAN.test(chapter)) {
    const loc = part && !NUMBERED.some((p) => part.startsWith(p)) ? `${part}, ${chapter}` : chapter;
    ref = `${base}, ${loc}`;
  } else {
    const title = `“${smartTitleCase(chapter)}”`;
    ref = `${base}, ${part ? smartTitleCase(part) + " — " + title : title}`;
  }
  if (h.source_page) ref += ` (p. ${h.source_page} in this edition's source scan)`;
  return ref;
}

/* --------------------------------- search ------------------------------- */

const LANG_NAME = { en: "English", fr: "French", de: "German", es: "Spanish", it: "Italian" };

async function runSearch() {
  const query = $("query").value.trim();
  if (!query || !ready) return;
  $("go").disabled = true;
  statusEl.textContent = "Searching…";
  try {
    const params = { query, topK: Math.max(1, Math.min(50, parseInt($("topk").value) || 8)) };
    if ($("language").value) params.language = $("language").value;
    if ($("genre").value) params.genre = $("genre").value;
    if ($("author").value.trim()) params.author = $("author").value.trim();
    const { results } = await window.lit.search(params);
    render(results);
    statusEl.textContent = results.length ? `${results.length} passages` : "Nothing found.";
  } catch (e) {
    statusEl.textContent = e.message; statusEl.className = "error";
  } finally {
    $("go").disabled = false;
  }
}

function render(results) {
  const main = $("results");
  main.innerHTML = "";
  for (const h of results) {
    const card = document.createElement("div");
    card.className = "hit";
    const cite = document.createElement("div");
    cite.className = "cite";
    cite.textContent = citation(h);
    if (h.relevance != null) {
      const rel = document.createElement("span");
      rel.className = "rel";
      rel.textContent = `${Math.round(h.relevance * 100)}%`;
      cite.appendChild(rel);
    }
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = h.text;

    const actions = document.createElement("div");
    actions.className = "actions";
    if (TIER >= 1) {
      const tBtn = document.createElement("button");
      tBtn.textContent = "Translate";
      tBtn.onclick = () => translateHit(h, card, tBtn);
      actions.appendChild(tBtn);
    }
    if (TIER >= 2) {
      const rBtn = document.createElement("button");
      rBtn.textContent = "Read aloud";
      rBtn.onclick = () => openKaraoke(h, rBtn);
      actions.appendChild(rBtn);
    }
    card.append(cite, text, actions);
    main.appendChild(card);
  }
}

/* -------------------------------- translate ----------------------------- */

async function translateHit(h, card, btn) {
  const target = $("target").value;
  const existing = card.querySelector(".translation");
  if (existing) { existing.remove(); btn.textContent = "Translate"; return; }
  btn.disabled = true;
  btn.textContent = "Translating…";
  try {
    const { translation, cached, echoed } = await window.lit.translate(
      { text: h.text, source: h.language, target });
    const div = document.createElement("div");
    div.className = "translation";
    const label = document.createElement("span");
    label.className = "tlabel";
    label.textContent = echoed
      ? "⚠ the local model could not translate this passage (Latin/Greek are its weak spot)"
      : `${LANG_NAME[target] || target} — machine translation (local model${cached ? ", cached" : ""})`;
    div.appendChild(label);
    div.appendChild(document.createTextNode(translation));
    card.appendChild(div);
    btn.textContent = "Hide translation";
  } catch (e) {
    statusEl.textContent = `Translation failed: ${e.message}`; statusEl.className = "error";
    btn.textContent = "Translate";
  } finally {
    btn.disabled = false;
  }
}

/* ----------------------------- karaoke read-aloud ------------------------ */

let audio = null, kWords = [], kSpans = [], kRaf = 0, kHit = null;

async function openKaraoke(h, btn) {
  btn.disabled = true; btn.textContent = "Preparing voice…";
  try {
    const engine = $("voice-engine") ? $("voice-engine").value : "auto";
    if (engine === "dia" && h.language === "en")
      statusEl.textContent = "Dia generation is slow (~1 min per paragraph, cached afterwards)…";
    const { audio: wavPath, words, voice } = await window.lit.speak(
      { text: h.text, language: h.language, engine });
    kHit = h; kWords = words;
    $("karaoke-citation").textContent = citation(h);
    const box = $("karaoke-text");
    box.innerHTML = "";
    kSpans = words.map((w) => {
      const s = document.createElement("span");
      s.textContent = w.w;
      box.appendChild(s);
      box.appendChild(document.createTextNode(" "));
      return s;
    });
    $("karaoke-note").textContent = `voice: ${voice}`;
    $("karaoke").classList.remove("hidden");
    audio = new Audio("lit-audio://" + wavPath);
    audio.addEventListener("ended", () => cancelAnimationFrame(kRaf));
    audio.play();
    tick();
  } catch (e) {
    statusEl.textContent = `Read-aloud failed: ${e.message}`; statusEl.className = "error";
  } finally {
    btn.disabled = false; btn.textContent = "Read aloud";
  }
}

function tick() {
  const t = audio.currentTime;
  kWords.forEach((w, i) => {
    const cls = t >= w.end ? "said" : (t >= w.start ? "now" : "");
    if (kSpans[i].className !== cls) kSpans[i].className = cls;
  });
  kRaf = requestAnimationFrame(tick);
}

$("karaoke-close").onclick = () => {
  audio?.pause(); cancelAnimationFrame(kRaf);
  $("karaoke").classList.add("hidden");
};
$("karaoke-replay").onclick = () => {
  if (!audio) return;
  audio.currentTime = 0; audio.play(); tick();
};

/* ------------------------------- video export ---------------------------
   Re-plays the reading while drawing the karaoke onto a canvas and
   recording canvas + audio into a .webm. What you hear is what you get. */

$("karaoke-export").onclick = async () => {
  if (!audio || !kHit) return;
  const btn = $("karaoke-export");
  btn.disabled = true; btn.textContent = "Recording…";
  try {
    const W = 1280, H = 720, PAD = 90;
    const canvas = document.createElement("canvas");
    canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext("2d");

    // layout words once
    ctx.font = "30px Georgia";
    const lines = []; let line = [], x = 0;
    const maxW = W - PAD * 2, space = ctx.measureText(" ").width;
    kWords.forEach((w, i) => {
      const width = ctx.measureText(w.w).width;
      if (x + width > maxW && line.length) { lines.push(line); line = []; x = 0; }
      line.push({ i, w: w.w, x, width });
      x += width + space;
    });
    if (line.length) lines.push(line);
    const lineH = 52, textTop = Math.max(150, (H - lines.length * lineH) / 2);

    function draw(t) {
      ctx.fillStyle = "#161412"; ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#c9a227"; ctx.font = "italic 21px Georgia";
      ctx.fillText(citation(kHit), PAD, 70, maxW);
      ctx.font = "30px Georgia";
      lines.forEach((ln, li) => {
        ln.forEach(({ i, w, x, width }) => {
          const y = textTop + li * lineH;
          const word = kWords[i];
          if (t >= word.start && t < word.end) {
            ctx.fillStyle = "#c9a227";
            ctx.fillRect(PAD + x - 4, y - 30, width + 8, 42);
            ctx.fillStyle = "#1c1911";
          } else {
            ctx.fillStyle = t >= word.end ? "#9a8f80" : "#e8e2d9";
          }
          ctx.fillText(w, PAD + x, y);
        });
      });
    }

    // mix canvas video + wav audio into one stream
    const actx = new AudioContext();
    const src = actx.createMediaElementSource(audio);
    const dest = actx.createMediaStreamDestination();
    src.connect(dest); src.connect(actx.destination);
    const stream = new MediaStream([
      ...canvas.captureStream(30).getVideoTracks(),
      ...dest.stream.getAudioTracks(),
    ]);
    const rec = new MediaRecorder(stream, { mimeType: "video/webm;codecs=vp9,opus" });
    const parts = [];
    rec.ondataavailable = (e) => e.data.size && parts.push(e.data);
    const done = new Promise((res) => (rec.onstop = res));

    audio.pause(); audio.currentTime = 0;
    rec.start(250);
    const drawLoop = () => { draw(audio.currentTime); if (!audio.ended) requestAnimationFrame(drawLoop); };
    await audio.play();
    drawLoop();
    await new Promise((res) => audio.addEventListener("ended", res, { once: true }));
    rec.stop();
    await done;

    const blob = new Blob(parts, { type: "video/webm" });
    const name = `${kHit.author.split(" ").pop()}_${(kHit.title || "reading").slice(0, 30).replace(/[^\w]+/g, "-")}.webm`;
    const saved = await window.lit.saveVideo(await blob.arrayBuffer(), name);
    $("karaoke-note").textContent = saved ? `Saved: ${saved}` : "Export cancelled";
  } catch (e) {
    $("karaoke-note").textContent = `Export failed: ${e.message}`;
  } finally {
    btn.disabled = false; btn.textContent = "Export video";
  }
};

/* --------------------------------- history ------------------------------ */

async function toggleHistory() {
  const panel = $("history-panel");
  if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
  await renderHistory();
  panel.classList.remove("hidden");
}

async function renderHistory() {
  const panel = $("history-panel");
  const { entries } = await window.lit.history({ limit: 50 });
  panel.innerHTML = "";
  if (!entries.length) { panel.textContent = "No searches yet."; return; }

  const head = document.createElement("div");
  head.className = "hist-head";
  const count = document.createElement("span");
  count.textContent = `${entries.length} past searches`;
  const clear = document.createElement("button");
  clear.className = "ghost";
  clear.textContent = "Clear all";
  clear.onclick = async () => {
    if (!confirm("Delete the entire search history?")) return;
    await window.lit.historyClear();
    await renderHistory();
  };
  head.append(count, clear);
  panel.appendChild(head);

  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "hist-entry";
    const ts = document.createElement("span");
    ts.className = "ts"; ts.textContent = e.ts.slice(5, 16);
    const q = document.createElement("span");
    q.className = "q"; q.textContent = e.query;
    const meta = document.createElement("span");
    meta.className = "meta";
    const bits = [];
    if (e.params.language) bits.push(e.params.language);
    if (e.params.genre) bits.push(e.params.genre);
    if (e.params.author) bits.push(e.params.author);
    bits.push(`${e.results.length} hits`);
    meta.textContent = bits.join(" · ");
    const del = document.createElement("button");
    del.className = "hist-del";
    del.textContent = "×";
    del.title = "Delete this entry";
    del.onclick = async (ev) => {
      ev.stopPropagation(); // don't also load the entry
      await window.lit.historyDelete(e.id);
      await renderHistory();
    };
    row.append(ts, q, meta, del);
    row.onclick = () => {
      // restore the stored results and the controls that produced them
      $("query").value = e.query;
      $("language").value = e.params.language || "";
      $("genre").value = e.params.genre || "";
      $("author").value = e.params.author || "";
      if (e.params.topK) $("topk").value = e.params.topK;
      render(e.results);
      statusEl.textContent = `Recalled from history (${e.ts})`;
      panel.classList.add("hidden");
    };
    panel.appendChild(row);
  }
}

/* -------------------------------- add book ------------------------------ */

const GENRES = ["prose", "poetry"];

async function searchBooks() {
  const q = $("book-query").value.trim();
  if (!q) return;
  $("book-go").disabled = true;
  const box = $("book-results");
  box.textContent = "Searching Project Gutenberg…";
  try {
    const params = { query: q };
    if ($("book-lang").value) params.language = $("book-lang").value;
    const { candidates } = await window.lit.bookSearch(params);
    box.innerHTML = "";
    if (!candidates.length) { box.textContent = "Nothing found on Gutenberg."; return; }
    for (const c of candidates) box.appendChild(bookRow(c));
  } catch (e) {
    box.textContent = `Gutenberg search failed: ${e.message}`;
  } finally {
    $("book-go").disabled = false;
  }
}

function bookRow(c) {
  const row = document.createElement("div");
  row.className = "book-row";
  const info = document.createElement("div");
  info.className = "book-info";
  info.innerHTML = `<span class="book-title"></span><span class="book-meta"></span>`;
  info.querySelector(".book-title").textContent = `${c.author} — ${c.title}`;
  info.querySelector(".book-meta").textContent =
    `#${c.id} · ${c.language} · ${c.downloads.toLocaleString()} downloads`;

  const year = document.createElement("input");
  year.type = "number"; year.placeholder = "Year"; year.className = "book-year";
  const genre = document.createElement("select");
  for (const g of GENRES) {
    const o = document.createElement("option");
    o.value = g; o.textContent = g;
    genre.appendChild(o);
  }
  const btn = document.createElement("button");
  if (c.indexed) { btn.textContent = "In index"; btn.disabled = true; }
  else {
    btn.textContent = "Add";
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = "Adding…";
      try {
        const res = await window.lit.bookAdd({
          gutenbergId: c.id, url: c.url, title: c.title, author: c.author,
          language: c.language, year: parseInt(year.value) || 0,
          genre: genre.value,
        });
        if (res.added) {
          btn.textContent = "✓ Added";
          statusEl.textContent = `${c.title} indexed (${res.words.toLocaleString()} words) — searchable now.`;
          statusEl.className = "";
        } else {
          btn.textContent = "Add"; btn.disabled = false;
          statusEl.textContent = `Not added: ${res.reason}`;
        }
      } catch (e) {
        btn.textContent = "Add"; btn.disabled = false;
        statusEl.textContent = `Add failed: ${e.message}`; statusEl.className = "error";
      }
    };
  }
  row.append(info, year, genre, btn);
  return row;
}

/* --------------------------------- wiring ------------------------------- */

$("go").onclick = runSearch;
$("query").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
$("history-btn").onclick = toggleHistory;
$("addbook-btn").onclick = () => $("addbook-panel").classList.toggle("hidden");
$("book-go").onclick = searchBooks;
$("book-query").addEventListener("keydown", (e) => { if (e.key === "Enter") searchBooks(); });

// cursor lands in the search bar on open and whenever the window refocuses
$("query").focus();
window.lit.onFocusSearch(() => {
  if (document.activeElement?.tagName !== "INPUT") $("query").focus();
});
