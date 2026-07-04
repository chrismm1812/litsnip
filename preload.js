const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("lit", {
  profile: process.env.LIT_PROFILE || "full",
  search: (params) => ipcRenderer.invoke("search", params),
  history: (params) => ipcRenderer.invoke("history", params),
  historyDelete: (entryId) => ipcRenderer.invoke("history-delete", entryId),
  historyClear: () => ipcRenderer.invoke("history-clear"),
  bookSearch: (params) => ipcRenderer.invoke("book-search", params),
  bookAdd: (params) => ipcRenderer.invoke("book-add", params),
  onFocusSearch: (fn) => ipcRenderer.on("focus-search", fn),
  translate: (params) => ipcRenderer.invoke("translate", params),
  speak: (params) => ipcRenderer.invoke("speak", params),
  saveVideo: (buf, name) => ipcRenderer.invoke("save-video", buf, name),
  onEvent: (fn) => ipcRenderer.on("sidecar-event", (_e, msg) => fn(msg)),
});
