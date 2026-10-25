// "Download this video" button that floats over playing / hovered videos, like IDM's panel.
(() => {
  if (window.__ydmLoaded) return;
  window.__ydmLoaded = true;

  const MIN_W = 180, MIN_H = 100;
  const YTDLP_SITES = /(^|\.)(youtube\.com|youtube-nocookie\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|twitter\.com|x\.com|facebook\.com|instagram\.com|tiktok\.com|reddit\.com|bilibili\.com|soundcloud\.com|streamable\.com|rumble\.com|bitchute\.com|ok\.ru|vk\.com)$/i;
  let enabled = true;
  let host, shadow, bar, menu, toast;
  let current = null;           // the <video> the bar is attached to
  let hideTimer = 0, pinned = false;
  const dismissed = new WeakSet();

  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => res(chrome.runtime.lastError ? { ok: false, error: chrome.runtime.lastError.message } : r)); }
    catch (e) { res({ ok: false, error: String(e) }); } // extension reloaded: page needs a refresh
  });

  chrome.storage.local.get(["overlay"], (s) => { enabled = s.overlay !== false; });
  chrome.storage.onChanged.addListener((c) => { if (c.overlay) { enabled = c.overlay.newValue !== false; if (!enabled) hide(true); } });

  const fmtSize = (n) => {
    if (!(n > 0)) return "";
    const u = ["B", "KB", "MB", "GB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
  };

  const SVG_NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    for (const c of children) e.append(c);
    return e;
  }

  function build() {
    if (host) return;
    host = document.createElement("ydm-overlay");
    host.style.cssText = "all:initial;position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;";
    shadow = host.attachShadow({ mode: "closed" });
    // Built with DOM calls, never innerHTML: YouTube (and other sites) enforce Trusted Types,
    // which also applies to content scripts and would throw on any HTML string.
    const style = document.createElement("style");
    style.textContent = `
        :host { all: initial; }
        * { box-sizing: border-box; font: 13px/1.3 "Segoe UI", system-ui, sans-serif; }
        .bar { position: fixed; display: none; align-items: center; gap: 2px; pointer-events: auto;
               background: linear-gradient(#2b8ae8, #1565c0); color: #fff; border-radius: 6px;
               box-shadow: 0 2px 10px rgba(0,0,0,.45); user-select: none; overflow: hidden; }
        .bar.show { display: flex; animation: pop .15s ease-out; }
        @keyframes pop { from { transform: translateY(-4px); opacity: 0 } }
        .main { display: flex; align-items: center; gap: 7px; padding: 6px 10px; cursor: pointer; border: 0;
                background: transparent; color: inherit; }
        .main:hover, .x:hover { background: rgba(255,255,255,.15); }
        .x { border: 0; background: transparent; color: #dfe9f5; padding: 6px 8px; cursor: pointer;
             border-left: 1px solid rgba(255,255,255,.25); font-size: 14px; }
        svg { width: 16px; height: 16px; fill: #fff; }
        .menu { position: fixed; display: none; pointer-events: auto; min-width: 320px; max-width: 480px;
                max-height: min(480px, 75vh); overflow: auto; background: #fff; color: #222; border-radius: 6px;
                box-shadow: 0 6px 24px rgba(0,0,0,.35); padding: 4px 0; }
        .menu.show { display: block; }
        .h { padding: 6px 12px 3px; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #777; }
        .it { display: flex; justify-content: space-between; gap: 12px; padding: 7px 12px; cursor: pointer; }
        .it:hover { background: #e8f1fc; }
        .it .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .it .s { color: #777; flex: none; }
        .it .d { color: #999; font-size: 11px; margin-left: 8px; }
        .toggle { cursor: pointer; border-top: 1px solid #eee; margin-top: 4px; padding-top: 8px; }
        .toggle:hover { color: #1565c0; }
        .raw { display: none; }
        .raw.open { display: block; }
        .muted { padding: 8px 12px; color: #777; }
        .toast { position: fixed; display: none; pointer-events: none; background: #222; color: #fff; padding: 8px 12px;
                 border-radius: 6px; box-shadow: 0 2px 10px rgba(0,0,0,.4); max-width: 420px; }
        .toast.show { display: block; }
        .toast.err { background: #b71c1c; }
    `;
    const icon = document.createElementNS(SVG_NS, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    for (const d of ["M11 3h2v10.2l3.6-3.6 1.4 1.4-6 6-6-6 1.4-1.4 3.6 3.6z", "M5 19h14v2H5z"]) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      icon.appendChild(path);
    }
    shadow.append(
      style,
      el("div", { class: "bar" }, [
        el("button", { class: "main", title: "Download with YDM" }, [icon, el("span", {}, ["Download this video"])]),
        el("button", { class: "x", title: "Hide for this video" }, ["✕"]),
      ]),
      el("div", { class: "menu" }),
      el("div", { class: "toast" }),
    );
    bar = shadow.querySelector(".bar");
    menu = shadow.querySelector(".menu");
    toast = shadow.querySelector(".toast");
    shadow.querySelector(".main").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
    shadow.querySelector(".x").addEventListener("click", (e) => {
      e.stopPropagation();
      if (current) dismissed.add(current);
      hide(true);
    });
    for (const el of [bar, menu]) {
      el.addEventListener("mouseenter", () => { pinned = true; clearTimeout(hideTimer); });
      el.addEventListener("mouseleave", () => { pinned = false; scheduleHide(); });
    }
    mount();
  }

  function mount() {
    const parent = document.fullscreenElement || document.webkitFullscreenElement || document.documentElement;
    if (host && host.parentNode !== parent) parent.appendChild(host);
  }

  function usable(v) {
    if (!(v instanceof HTMLVideoElement) || dismissed.has(v)) return false;
    const r = v.getBoundingClientRect();
    return r.width >= MIN_W && r.height >= MIN_H && getComputedStyle(v).visibility !== "hidden";
  }

  function place() {
    if (!current || !current.isConnected) return hide(true);
    const r = current.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) return hide(true);
    const w = bar.offsetWidth || 190;
    bar.style.top = `${Math.max(r.top + 10, 6)}px`;
    bar.style.left = `${Math.min(r.right - w - 10, innerWidth - w - 6)}px`;
    if (menu.classList.contains("show")) {
      const b = bar.getBoundingClientRect();
      menu.style.top = `${b.bottom + 4}px`;
      menu.style.left = `${Math.max(6, Math.min(b.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 6))}px`;
    }
  }

  function show(video, ms = 0) {
    if (!enabled) return;
    build();
    mount();
    if (current !== video) closeMenu();
    current = video;
    bar.classList.add("show");
    place();
    clearTimeout(hideTimer);
    if (ms) hideTimer = setTimeout(() => { if (!pinned) hide(); }, ms);
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { if (!pinned && !menu.classList.contains("show")) hide(); }, 1800);
  }

  function hide(force) {
    if (!bar || (!force && pinned)) return;
    bar.classList.remove("show");
    closeMenu();
  }

  // ------------------------------------------------------------- menu
  function closeMenu() { if (menu) { menu.classList.remove("show"); menu.replaceChildren(); } }

  function toggleMenu() {
    if (menu.classList.contains("show")) return closeMenu();
    menu.classList.add("show");
    fillMenu();
  }

  function item(label, size, onClick, detail = "") {
    const name = el("span", { class: "n" }, [label]);
    if (detail) name.append(el("span", { class: "d" }, [detail]));
    const d = el("div", { class: "it" }, [name, el("span", { class: "s" }, [size])]);
    d.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return d;
  }
  const row = (label, size, onClick) => menu.appendChild(item(label, size, onClick));
  const heading = (t) => { const h = document.createElement("div"); h.className = "h"; h.textContent = t; menu.appendChild(h); };
  const note = (t) => { const n = document.createElement("div"); n.className = "muted"; n.textContent = t; menu.appendChild(n); return n; };

  async function fillMenu() {
    menu.replaceChildren();
    note("Looking for media…");
    const res = await send({ type: "getMedia" });
    menu.replaceChildren();
    const items = (res && res.items) || [];
    const title = document.title.replace(/[\\/:*?"<>|]+/g, " ").trim().slice(0, 120) || "video";
    const src = current && current.currentSrc;
    const siteNeedsYtdlp = YTDLP_SITES.test(location.hostname) || (res && res.ytdlpSite);

    if (siteNeedsYtdlp) {
      heading("Choose quality");
      const wait = note("Fetching available qualities…");
      place();
      loadQualities(wait, title);
    }
    const direct = items.filter((m) => m.kind === "file").sort((a, b) => (b.size || 0) - (a.size || 0));
    const streams = items.filter((m) => m.kind !== "file");
    if (src && /^https?:/.test(src) && !direct.some((m) => m.url === src)) direct.unshift({ url: src, kind: "file", size: -1, name: "Playing video" });
    if (direct.length || streams.length) heading(siteNeedsYtdlp ? "Other detected media" : "Detected media");
    for (const m of direct) {
      const ext = (m.name.split(".").pop() || "mp4").slice(0, 5);
      row(`${m.name === "Playing video" ? "This video" : m.name}`, fmtSize(m.size) || ext.toUpperCase(),
          () => download({ url: m.url, filename: m.name.includes(".") ? m.name : `${title}.mp4`, size: m.size }));
    }
    for (const m of streams) {
      const label = m.kind === "hls" ? "HLS stream (best quality)" : "DASH stream";
      row(`${label} — ${m.name}`, m.kind.toUpperCase(),
          () => download({ url: m.url, kind: m.kind === "hls" ? "hls" : "ytdlp", filename: `${title}.mp4` }));
    }
    if (!siteNeedsYtdlp && !direct.length && !streams.length) {
      heading("No direct file seen yet");
      note("Play the video for a moment, or try:");
      row("Detect with yt-dlp", "", () => { closeMenu(); menu.classList.add("show"); heading("Choose quality"); loadQualities(note("Fetching…"), title); });
    }
    place();
  }

  async function loadQualities(placeholder, title) {
    const res = await send({ type: "formats", url: location.href });
    if (!placeholder.isConnected) return;
    if (!res || res.error || !res.formats) {
      const msg = (res && res.error) || "YDM did not answer";
      placeholder.textContent = /not found|native messaging host/i.test(msg)
        ? "YDM app is not installed or not registered (run install.ps1)."
        : `Could not list qualities: ${msg.slice(0, 140)}`;
      return;
    }
    const anchor = placeholder;
    const name = (res.title || title).replace(/[\\/:*?"<>|]+/g, " ").slice(0, 120);
    const entry = (f) => item(f.label, fmtSize(f.size) || f.ext.toUpperCase(), () =>
      download({ url: location.href, kind: "ytdlp", format: f.format, filename: `${name}.${f.ext}`, title: res.title }), f.detail);
    const group = (g) => res.formats.filter((f) => (f.group || "video") === g);
    for (const f of group("video")) anchor.before(entry(f));
    const audio = group("audio");
    if (audio.length) {
      anchor.before(el("div", { class: "h" }, ["Audio only"]));
      for (const f of audio) anchor.before(entry(f));
    }
    // Every raw stream yt-dlp reports, folded away so the main list stays readable.
    const raw = group("all");
    if (raw.length) {
      const list = el("div", { class: "raw" }, raw.map(entry));
      const toggle = el("div", { class: "h toggle" }, [`▸ All formats (${raw.length})`]);
      toggle.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = list.classList.toggle("open");
        toggle.textContent = `${open ? "▾" : "▸"} All formats (${raw.length})`;
      });
      anchor.before(toggle, list);
    }
    anchor.remove();
    place();
  }

  async function download(item) {
    closeMenu();
    const r = await send({ type: "download", item: { referer: location.href, pageUrl: location.href, ...item } });
    flash(r && r.ok ? "Sent to YDM ✓" : `YDM: ${(r && r.error) || "no answer"}`, !(r && r.ok));
  }

  function flash(text, err) {
    build();
    toast.textContent = text;
    toast.classList.toggle("err", !!err);
    const b = bar.getBoundingClientRect();
    toast.style.top = `${(b.bottom || 40) + 6}px`;
    toast.style.left = `${Math.max(6, (b.left || 20))}px`;
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), err ? 5000 : 2200);
  }

  // ------------------------------------------------------------- triggers
  let raf = 0, lastX = 0, lastY = 0;
  document.addEventListener("mousemove", (e) => {
    lastX = e.clientX; lastY = e.clientY;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      if (!enabled) return;
      // Players often cover the <video> with control layers, so hit-test by geometry.
      const hit = [...document.querySelectorAll("video")].find((v) => {
        if (!usable(v)) return false;
        const r = v.getBoundingClientRect();
        return lastX >= r.left && lastX <= r.right && lastY >= r.top && lastY <= r.bottom;
      });
      if (hit) show(hit);
      else if (bar && bar.classList.contains("show") && !pinned) scheduleHide();
    });
  }, { passive: true, capture: true });

  // When a video starts playing, show the bar for a few seconds like IDM does.
  document.addEventListener("play", (e) => { if (usable(e.target)) show(e.target, 5000); }, true);
  // Autoplaying videos (YouTube) start before this script loads, and YouTube swaps videos
  // without reloading the page, so also look for an already-playing video.
  const showPlaying = () => {
    const v = [...document.querySelectorAll("video")].find((x) => !x.paused && usable(x));
    if (v) show(v, 5000);
  };
  setTimeout(showPlaying, 800);
  document.addEventListener("yt-navigate-finish", () => setTimeout(showPlaying, 800));
  addEventListener("scroll", () => bar && bar.classList.contains("show") && place(), { passive: true, capture: true });
  addEventListener("resize", () => bar && bar.classList.contains("show") && place(), { passive: true });
  document.addEventListener("fullscreenchange", () => { mount(); if (bar && bar.classList.contains("show")) setTimeout(place, 50); });
  document.addEventListener("click", (e) => { if (host && !e.composedPath().includes(host)) closeMenu(); }, true);

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "mediaUpdated" && menu && menu.classList.contains("show")) fillMenu();
    if (msg.type === "openMenu") {
      const v = [...document.querySelectorAll("video")].filter(usable)
        .sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
      if (v) { show(v); toggleMenu(); }
    }
  });
})();
