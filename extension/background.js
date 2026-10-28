// YDM service worker: talks to the desktop app through the native messaging host,
// takes over browser downloads, and sniffs media requests so the in-page button
// can offer the real video file.
const HOST = "com.ydm.host";
const DEFAULTS = {
  enabled: true,          // take over browser downloads
  overlay: true,          // show the button over videos
  extensions: ["zip","rar","7z","gz","tar","bz2","xz","iso","exe","msi","apk","dmg","pdf","doc","docx","xls","xlsx","ppt","pptx","epub",
               "mp3","wav","flac","aac","m4a","ogg","opus","mp4","mkv","avi","mov","wmv","flv","webm","m4v","3gp","mpg","mpeg"],
  minSizeKB: 0,
};
// Sites whose players hide the real file (DASH fragments, signed URLs): hand the page URL to yt-dlp.
const YTDLP_SITES = /(^|\.)(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|twitter\.com|x\.com|facebook\.com|instagram\.com|tiktok\.com|reddit\.com|bilibili\.com|soundcloud\.com|streamable\.com|rumble\.com|bitchute\.com|ok\.ru|vk\.com)$/i;
const FRAGMENT_URL = /googlevideo\.com\/videoplayback|\.m4s(\?|$)|\/range\/\d|[?&](range|bytestart)=/i;

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

function native(type, payload = {}) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(HOST, { type, payload, launch: payload.launch }, (reply) => {
        if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
        else resolve(reply || { ok: false, error: "no reply" });
      });
    } catch (e) {
      resolve({ ok: false, error: String(e) });
    }
  });
}

async function cookiesFor(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    return list.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {
    return "";
  }
}

// Every cookie of the site (all subdomains), for yt-dlp's --cookies file. Sites such as Instagram,
// Facebook and X only hand video data to signed-in visitors, and their API calls go to other
// subdomains than the page, so the page's own Cookie header is not enough.
async function cookieJar(url) {
  try {
    const host = new URL(url).hostname;
    const domain = host.split(".").slice(-2).join(".");
    const list = await chrome.cookies.getAll({ domain });
    return list.map((c) => ({ domain: c.domain, path: c.path, secure: c.secure, hostOnly: c.hostOnly,
                              expires: Math.floor(c.expirationDate || 0), name: c.name, value: c.value }));
  } catch {
    return [];
  }
}

async function sendDownload({ url, referer = "", filename = "", kind = "http", size = -1, pageUrl = "", format = "", title = "", silent = false }) {
  const payload = {
    url, referer, filename, kind, size, page_url: pageUrl, format, title, silent,
    cookies: url.startsWith("http") ? await cookiesFor(url) : "",
    user_agent: navigator.userAgent,
  };
  if (kind === "ytdlp") payload.cookie_jar = await cookieJar(pageUrl || url);
  return native("add", payload);
}

// ---------------------------------------------------------------- media sniffing
const mediaKey = (tabId) => `media:${tabId}`;

async function getMedia(tabId) {
  const k = mediaKey(tabId);
  return (await chrome.storage.session.get(k))[k] || [];
}

async function setMedia(tabId, items) {
  await chrome.storage.session.set({ [mediaKey(tabId)]: items });
  chrome.action.setBadgeText({ tabId, text: items.length ? String(items.length) : "" });
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#1e88e5" });
}

function header(headers, name) {
  const h = headers.find((x) => x.name.toLowerCase() === name);
  return h ? h.value : "";
}

function classify(url, ctype) {
  const path = url.split(/[?#]/)[0].toLowerCase();
  if (/mpegurl/.test(ctype) || path.endsWith(".m3u8")) return "hls";
  if (/dash\+xml/.test(ctype) || path.endsWith(".mpd")) return "dash";
  if (/^(video|audio)\//.test(ctype) || /\.(mp4|webm|mkv|flv|mov|m4v|mp3|m4a|ogg|opus|wav|flac)$/.test(path)) {
    if (/mp2t/.test(ctype) || path.endsWith(".ts")) return null; // an HLS segment
    return "file";
  }
  return null;
}

const lock = new Map(); // serialize writes per tab
chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (d.tabId < 0 || d.statusCode >= 400 || FRAGMENT_URL.test(d.url) || !/^https?:/.test(d.url)) return;
    const ctype = header(d.responseHeaders || [], "content-type").toLowerCase();
    const kind = classify(d.url, ctype);
    if (!kind) return;
    let size = -1;
    const cr = header(d.responseHeaders, "content-range").match(/\/(\d+)\s*$/);
    if (cr) size = +cr[1];
    else if (header(d.responseHeaders, "content-length")) size = +header(d.responseHeaders, "content-length");
    if (kind === "file" && size >= 0 && size < 300 * 1024) return; // thumbnails, beeps, previews
    const prev = lock.get(d.tabId) || Promise.resolve();
    const next = prev.then(async () => {
      const items = await getMedia(d.tabId);
      const base = d.url.split("#")[0];
      if (items.some((m) => m.url === base)) return;
      const name = decodeURIComponent(base.split("?")[0].split("/").pop() || "video");
      items.unshift({ url: base, kind, size, mime: ctype, name, frameId: d.frameId, ts: Date.now(), initiator: d.initiator || "" });
      await setMedia(d.tabId, items.slice(0, 30));
      chrome.tabs.sendMessage(d.tabId, { type: "mediaUpdated" }).catch(() => {});
    });
    lock.set(d.tabId, next.catch(() => {}));
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object", "sub_frame"] },
  ["responseHeaders"]
);

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url) setMedia(tabId, []); // new page (also SPA navigations)
});
chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(mediaKey(tabId)));

// ---------------------------------------------------------------- download takeover
const passThrough = new Set(); // ids we resumed in the browser after the app was unreachable

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  suggest(); // never block the browser's own naming
  handleBrowserDownload(item);
});

async function handleBrowserDownload(item) {
  const s = await settings();
  if (!s.enabled || passThrough.has(item.id) || item.byExtensionId === chrome.runtime.id) return;
  const url = item.finalUrl || item.url;
  if (!/^(https?|ftp):/.test(url)) return; // blob:, data:, file: stay in the browser
  const ext = (item.filename.split(".").pop() || "").toLowerCase();
  if (!s.extensions.includes(ext)) return;
  if (s.minSizeKB && item.fileSize > 0 && item.fileSize < s.minSizeKB * 1024) return;

  await chrome.downloads.pause(item.id).catch(() => {});
  const reply = await sendDownload({
    url,
    referer: item.referrer || "",
    filename: item.filename.split(/[\\/]/).pop(),
    size: item.fileSize || item.totalBytes || -1,
  });
  if (reply.ok) {
    await chrome.downloads.cancel(item.id).catch(() => {});
    await chrome.downloads.erase({ id: item.id }).catch(() => {});
  } else {
    passThrough.add(item.id);
    await chrome.downloads.resume(item.id).catch(() => {});
    console.warn("YDM unavailable, browser keeps the download:", reply.error);
  }
}

// ---------------------------------------------------------------- context menus
chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set({ ...DEFAULTS, ...(await chrome.storage.local.get(null)) });
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "link", title: "Download with YDM", contexts: ["link"] });
    chrome.contextMenus.create({ id: "media", title: "Download this media with YDM", contexts: ["video", "audio", "image"] });
    chrome.contextMenus.create({ id: "all", title: "Download all links with YDM", contexts: ["page", "selection"] });
    chrome.contextMenus.create({ id: "page", title: "Download video from this page (yt-dlp)", contexts: ["page", "video"] });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const referer = info.pageUrl || tab?.url || "";
  if (info.menuItemId === "link") return sendDownload({ url: info.linkUrl, referer });
  if (info.menuItemId === "media") {
    if (info.srcUrl && /^https?:/.test(info.srcUrl)) return sendDownload({ url: info.srcUrl, referer });
    return chrome.tabs.sendMessage(tab.id, { type: "openMenu" }).catch(() => {});
  }
  if (info.menuItemId === "page") return sendDownload({ url: referer, kind: "ytdlp", pageUrl: referer, referer });
  if (info.menuItemId === "all") {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (selectionOnly) => {
        let anchors = [...document.querySelectorAll("a[href]")];
        const sel = window.getSelection();
        if (selectionOnly && sel && sel.rangeCount) anchors = anchors.filter((a) => sel.containsNode(a, true));
        const media = [...document.querySelectorAll("img[src],video[src],source[src],audio[src]")].map((e) => e.src);
        return [...new Set([...anchors.map((a) => a.href), ...media])].filter((u) => /^(https?|ftp):/.test(u));
      },
      args: [!!info.selectionText],
    });
    return native("batch", { urls: result, referer, cookies: await cookiesFor(referer) });
  }
});

// ---------------------------------------------------------------- messages from content script / popup
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    const tabId = msg.tabId ?? sender.tab?.id;
    switch (msg.type) {
      case "getMedia": {
        const tab = sender.tab || (await chrome.tabs.get(tabId));
        const host = new URL(tab.url).hostname;
        return { items: await getMedia(tabId), ytdlpSite: YTDLP_SITES.test(host), settings: await settings() };
      }
      case "download":
        return sendDownload(msg.item);
      case "formats":
        return native("formats", { url: msg.url, referer: msg.url, cookie_jar: await cookieJar(msg.url) });
      case "ping":
        return native("ping", { launch: !!msg.launch });
      case "show":
        return native("show");
      default:
        return { ok: false, error: "unknown" };
    }
  })().then(reply, (e) => reply({ ok: false, error: String(e) }));
  return true;
});
