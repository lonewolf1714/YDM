const $ = (id) => document.getElementById(id);
const send = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, res));
const fmtSize = (n) => {
  if (!(n > 0)) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const s = await chrome.storage.local.get(["enabled", "overlay"]);
  $("enabled").checked = s.enabled !== false;
  $("overlay").checked = s.overlay !== false;
  $("enabled").onchange = (e) => chrome.storage.local.set({ enabled: e.target.checked });
  $("overlay").onchange = (e) => chrome.storage.local.set({ overlay: e.target.checked });

  send({ type: "ping" }).then((r) => {
    $("status").textContent = r && r.ok ? (r.ytdlp ? "connected" : "connected · no yt-dlp") : "app not running";
  });
  $("open").onclick = () => send({ type: "show" }).then(() => window.close());
  $("page").onclick = () => send({ type: "download", item: { url: tab.url, kind: "ytdlp", pageUrl: tab.url, referer: tab.url, title: tab.title } })
    .then((r) => { $("status").textContent = r && r.ok ? "sent ✓" : (r && r.error) || "failed"; });

  const { items = [] } = (await send({ type: "getMedia", tabId: tab.id })) || {};
  if (!items.length) return;
  const box = $("media");
  box.innerHTML = "";
  for (const m of items) {
    const row = document.createElement("div");
    row.className = "m";
    row.innerHTML = `<span></span><small></small><button>Download</button>`;
    row.querySelector("span").textContent = m.name;
    row.querySelector("span").title = m.url;
    row.querySelector("small").textContent = fmtSize(m.size) || m.kind.toUpperCase();
    row.querySelector("button").onclick = async () => {
      const kind = m.kind === "file" ? "http" : m.kind === "hls" ? "hls" : "ytdlp";
      const name = m.kind === "file" && m.name.includes(".") ? m.name : `${tab.title.replace(/[\\/:*?"<>|]+/g, " ").slice(0, 100)}.mp4`;
      const r = await send({ type: "download", item: { url: m.url, kind, filename: name, size: m.size, referer: tab.url, pageUrl: tab.url } });
      row.querySelector("button").textContent = r && r.ok ? "Sent ✓" : "Failed";
    };
    box.appendChild(row);
  }
})();
