// Littéraire — Electron main process. Owns the window and the long-lived
// Python sidecar (search / translate / speak over NDJSON on stdio).
const { app, BrowserWindow, ipcMain, dialog, protocol } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

let win = null;
let sidecar = null;
let nextId = 1;
const pending = new Map(); // id -> {resolve, reject}
let buf = "";

// A GUI launch doesn't inherit shell PATH additions, and several python3s
// may coexist (ARM Homebrew, Intel Homebrew, system) — only one has the ML
// stack. Probe candidates for torch instead of trusting an ordering.
function resolvePython() {
  if (process.env.LIT_PYTHON_BIN) return process.env.LIT_PYTHON_BIN;
  const { execFileSync } = require("child_process");
  for (const p of ["/usr/local/bin/python3", "/opt/homebrew/bin/python3", "/usr/bin/python3"]) {
    if (!fs.existsSync(p)) continue;
    try {
      execFileSync(p, ["-c", "import torch"], { timeout: 20000, stdio: "ignore" });
      return p;
    } catch { /* no torch here, keep looking */ }
  }
  return "python3";
}

function startSidecar() {
  // Python is an external process: it cannot read inside the asar archive,
  // so packaged builds unpack python/ and voices/ next to it. The replace
  // is a no-op in dev, where __dirname is a real directory.
  const appDir = __dirname.replace("app.asar", "app.asar.unpacked");
  const script = path.join(appDir, "python", "sidecar.py");
  sidecar = spawn(resolvePython(), [script], {
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      // writable data (translation/tts caches, history) must live outside
      // the app bundle or it's lost on every update
      LIT_CACHE_DIR: app.isPackaged
        ? app.getPath("userData")
        : path.join(__dirname, "cache"),
    },
  });
  sidecar.stdout.on("data", (chunk) => {
    buf += chunk.toString("utf8");
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.type) { // unsolicited: status / ready
        console.log(`[event] ${msg.type} ${msg.msg || msg.device || ""}`);
        win?.webContents.send("sidecar-event", msg);
        continue;
      }
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        msg.ok ? p.resolve(msg.data) : p.reject(new Error(msg.error));
      }
    }
  });
  sidecar.stderr.on("data", (d) => process.stderr.write(`[sidecar] ${d}`));
  sidecar.on("exit", (code) => {
    win?.webContents.send("sidecar-event", { type: "died", code });
    for (const p of pending.values()) p.reject(new Error("sidecar exited"));
    pending.clear();
    sidecar = null;
  });
}

function call(op, params, timeoutMs = 300000) {
  return new Promise((resolve, reject) => {
    if (!sidecar) return reject(new Error("sidecar not running"));
    const id = nextId++;
    pending.set(id, { resolve, reject });
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(`${op} timed out`)); }
    }, timeoutMs);
    sidecar.stdin.write(JSON.stringify({ id, op, ...params }) + "\n");
  });
}

ipcMain.handle("search", (_e, params) => call("search", params));
ipcMain.handle("history", (_e, params) => call("history", params || {}));
ipcMain.handle("history-delete", (_e, entryId) => call("history_delete", { entryId }));
ipcMain.handle("history-clear", () => call("history_clear", {}));
ipcMain.handle("book-search", (_e, params) => call("book_search", params, 60000));
ipcMain.handle("book-add", (_e, params) => call("book_add", params, 1800000)); // embedding takes minutes
ipcMain.handle("translate", (_e, params) => call("translate", params, 600000));
ipcMain.handle("speak", (_e, params) => call("speak", params, 1800000)); // Dia is slow on long passages
ipcMain.handle("save-video", async (_e, arrayBuffer, suggestedName) => {
  const { canceled, filePath } = await dialog.showSaveDialog(win, {
    defaultPath: path.join(app.getPath("downloads"), suggestedName),
    filters: [{ name: "Video", extensions: ["webm"] }],
  });
  if (canceled || !filePath) return null;
  fs.writeFileSync(filePath, Buffer.from(arrayBuffer));
  return filePath;
});

app.whenReady().then(() => {
  // let the renderer stream TTS wav files from disk
  protocol.registerFileProtocol("lit-audio", (request, cb) => {
    // lit-audio:///private/tmp/x.wav -> /private/tmp/x.wav
    cb({ path: decodeURIComponent(request.url.slice("lit-audio://".length)) });
  });
  // restore the last window size/position; default generously sized so a
  // full results page is readable without resizing
  const boundsFile = path.join(app.getPath("userData"), "window-bounds.json");
  let saved = {};
  try { saved = JSON.parse(fs.readFileSync(boundsFile, "utf8")); } catch {}
  win = new BrowserWindow({
    width: saved.width || 1240, height: saved.height || 960,
    x: saved.x, y: saved.y,
    minWidth: 900, minHeight: 700,
    title: "litsnip",
    titleBarStyle: "hiddenInset",
    backgroundColor: "#161412",
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  win.on("close", () => {
    try { fs.writeFileSync(boundsFile, JSON.stringify(win.getBounds())); } catch {}
  });
  win.on("focus", () => win.webContents.send("focus-search"));
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  startSidecar();
});

app.on("window-all-closed", () => {
  sidecar?.kill();
  app.quit();
});
