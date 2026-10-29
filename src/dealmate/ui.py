"""MCP App: the interactive card Claude.ai renders inside the conversation.

One HTML view serves every UI-bound tool; it picks what to draw from the tool
result's `view` field:
- "gallery": product cards with photos (browse_listings)
- "listing": one product with its photo, and an upload button for the seller
  (create_listing, show_listing)

The page talks to the host over postMessage JSON-RPC (MCP Apps, 2026-01-26):
ui/initialize -> ui/notifications/initialized, then it receives
ui/notifications/tool-result and calls app-only tools with tools/call.
Photos travel as data: URLs from the app-only tool app_get_photo, so they never
enter the model's context and the default CSP (img-src data:) allows them.
"""

from mcp.server.apps import Apps

APP_URI = "ui://dealmate/app.html"


def ui_meta(app_only: bool = False) -> dict:
    """`meta=` for a tool bound to the DealMate card. App-only tools are hidden from the model."""
    ui = {"resourceUri": APP_URI}
    if app_only:
        ui["visibility"] = ["app"]
    return {"ui": ui}


APP_HTML = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DealMate</title>
<style>
  :root { --bg:#ffffff; --card:#f6f7f9; --text:#1b1d21; --muted:#6b7280; --line:#e3e6ea;
          --accent:#2f6fde; --ok:#1f9d55; --warn:#c27a00; --off:#8a8f98; }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
          --bg:#171717; --card:#222326; --text:#ececec; --muted:#a0a4ab; --line:#33353a;
          --accent:#6c9cff; --ok:#3fbf75; --warn:#e0a63a; --off:#7d828a; } }
  :root[data-theme="dark"] { --bg:#171717; --card:#222326; --text:#ececec; --muted:#a0a4ab;
          --line:#33353a; --accent:#6c9cff; --ok:#3fbf75; --warn:#e0a63a; --off:#7d828a; }
  * { box-sizing: border-box; }
  body { margin:0; padding:12px; background:var(--bg); color:var(--text);
         font-family:-apple-system,"PingFang TC","Noto Sans TC",system-ui,sans-serif; font-size:14px; }
  h2 { font-size:15px; margin:0 0 10px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:10px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .ph { aspect-ratio:1/1; background:var(--line); display:flex; align-items:center;
        justify-content:center; color:var(--muted); font-size:12px; }
  .ph img { width:100%; height:100%; object-fit:cover; display:block; }
  .body { padding:10px; }
  .title { font-weight:600; line-height:1.3; margin-bottom:4px; }
  .price { font-size:16px; font-weight:700; }
  .meta { color:var(--muted); font-size:12px; margin-top:4px; line-height:1.4; }
  .badge { display:inline-block; font-size:11px; padding:1px 7px; border-radius:99px; color:#fff; margin-left:4px;
           vertical-align:2px; }
  .open { background:var(--ok); } .matched { background:var(--warn); } .sold,.closed { background:var(--off); }
  details { margin-top:6px; font-size:12px; } summary { cursor:pointer; color:var(--accent); }
  table { border-collapse:collapse; width:100%; margin-top:4px; }
  td { padding:2px 0; vertical-align:top; } td:first-child { color:var(--muted); width:42%; }
  .debug { border-top:1px dashed var(--line); margin-top:6px; padding-top:6px; }
  .one { display:grid; grid-template-columns:minmax(0,240px) 1fr; gap:14px; }
  @media (max-width:520px) { .one { grid-template-columns:1fr; } }
  .one .ph { border-radius:12px; }
  button { font:inherit; border:0; border-radius:8px; padding:9px 14px; cursor:pointer;
           background:var(--accent); color:#fff; margin-top:10px; margin-right:6px; }
  button.ghost { background:transparent; color:var(--accent); border:1px solid var(--line); }
  button:disabled { opacity:.6; cursor:default; }
  .msg { margin-top:8px; font-weight:600; }
  .empty { color:var(--muted); padding:20px 0; text-align:center; }
</style></head>
<body><div id="root" class="empty">載入中…</div>
<script>
(() => {
  // ---------- minimal MCP Apps bridge (postMessage JSON-RPC) ----------
  let nextId = 1; const pending = new Map();
  const send = msg => window.parent.postMessage(Object.assign({ jsonrpc: "2.0" }, msg), "*");
  const request = (method, params) => new Promise((resolve, reject) => {
    const id = nextId++; pending.set(id, { resolve, reject }); send({ id, method, params });
  });
  const notify = (method, params) => send({ method, params });

  window.addEventListener("message", ev => {
    const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
    if (m.id !== undefined && pending.has(m.id) && !m.method) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message || "error")) : p.resolve(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-result") render(parseResult(m.params));
    else if (m.method === "ui/notifications/host-context-changed") applyContext(m.params);
    else if (m.method === "ui/resource-teardown" && m.id !== undefined) send({ id: m.id, result: {} });
  });

  function applyContext(ctx) {
    if (ctx && ctx.theme) document.documentElement.dataset.theme = ctx.theme;
  }

  function parseResult(r) {
    if (!r) return null;
    if (r.structuredContent) return r.structuredContent;
    const text = (r.content || []).filter(c => c.type === "text").map(c => c.text).join("");
    try { return JSON.parse(text); } catch (e) { return { view: "error", error: text }; }
  }

  async function callTool(name, args) {
    const r = await request("tools/call", { name, arguments: args });
    const data = parseResult(r);
    if (r && r.isError) throw new Error(data && data.error ? data.error : "工具執行失敗");
    return data;
  }

  // Tell the host how tall we are, so the card is not cut off.
  new ResizeObserver(() => notify("ui/notifications/size-changed", {
    width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight,
  })).observe(document.documentElement);

  request("ui/initialize", {
    appInfo: { name: "DealMate", version: "1.0.0" }, appCapabilities: {}, protocolVersion: "2026-01-26",
  }).then(res => { applyContext(res && res.hostContext); notify("ui/notifications/initialized", {}); })
    .catch(() => notify("ui/notifications/initialized", {}));

  // ---------- rendering ----------
  const root = document.getElementById("root");
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const money = n => n == null ? "" : "NT$ " + Number(n).toLocaleString("en-US");
  const STATUS = { open: "在售", matched: "議價中", sold: "已售出", closed: "已下架" };
  const STATE = { matched: "已配對", negotiating: "議價中", waiting_user: "等本人決定",
                  agreed: "已談成", closed: "已成交", failed: "未成交" };
  const badge = s => `<span class="badge ${esc(s)}">${esc(STATUS[s] || s)}</span>`;
  const row = (k, v) => v === undefined || v === null || v === "" ? "" : `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`;

  function details(l) {
    let html = `<table>${row("編號", "#" + l.id)}${row("品類", l.category)}${row("狀況", l.condition)}
      ${row("地點", l.location)}${row("賣家", l.seller)}${row("上架時間", l.created_at)}</table>`;
    if (l.floor_price !== undefined) {
      html += `<div class="debug"><b>除錯資料</b><table>${row("最低價", money(l.floor_price))}
        ${row("自動接受價", money(l.auto_accept_price))}${row("賣家 email", l.seller_email)}</table>`;
      for (const n of l.negotiations || []) {
        html += `<table>${row("議價 #" + n.id, (STATE[n.state] || n.state) + "，第 " + n.round + " 輪")}
          ${row("買家 / 預算", n.buyer + " / " + money(n.budget))}
          ${row("賣家 / 買家出價", money(n.seller_offer) + " / " + money(n.buyer_offer))}
          ${row("談成價", money(n.agreed_price))}</table>`;
      }
      html += `</div>`;
    }
    return html;
  }

  async function loadPhoto(listingId, el) {
    try {
      const d = await callTool("app_get_photo", { listing_id: listingId });
      el.innerHTML = `<img alt="商品照片" src="${d.data_url}">`;
    } catch (e) { el.textContent = "照片載入失敗"; }
  }

  function renderGallery(d) {
    const ls = d.listings || [];
    if (!ls.length) { root.className = "empty"; root.textContent = "目前沒有商品。"; return; }
    root.className = "";
    root.innerHTML = `<h2>DealMate 商品（${ls.length}）${d.debug ? "・除錯模式" : ""}</h2><div class="grid">` +
      ls.map(l => `<div class="card"><div class="ph" id="ph-${l.id}">${l.has_photo ? "載入照片…" : "沒有照片"}</div>
        <div class="body"><div class="title">${esc(l.title)}${badge(l.status)}</div>
        <div class="price">${money(l.ask_price)}</div>
        <div class="meta">${esc([l.condition, l.location].filter(Boolean).join("・"))}</div>
        <details><summary>詳細資料</summary>${details(l)}</details></div></div>`).join("") + `</div>`;
    for (const l of ls) if (l.has_photo) loadPhoto(l.id, document.getElementById("ph-" + l.id));
  }

  function shrink(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const s = Math.min(1, 1280 / Math.max(img.width, img.height));
        const c = document.createElement("canvas");
        c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        resolve(c.toDataURL("image/jpeg", 0.85));
      };
      img.onerror = () => reject(new Error("讀不到這張圖片"));
      img.src = URL.createObjectURL(file);
    });
  }

  function renderListing(d) {
    const l = d.listing || {};
    root.className = "";
    root.innerHTML = `${d.headline ? `<h2>${esc(d.headline)}</h2>` : ""}<div class="one">
      <div class="ph" id="ph">${l.has_photo ? "載入照片…" : "還沒有照片"}</div>
      <div><div class="title" style="font-size:16px">${esc(l.title)}${badge(l.status)}</div>
        <div class="price">${money(l.ask_price)}</div>${details(l)}
        <div id="up"></div></div></div>`;
    const ph = document.getElementById("ph");
    if (l.has_photo) loadPhoto(l.id, ph);
    if (!d.upload_token) return;

    const up = document.getElementById("up");
    up.innerHTML = `<input type="file" accept="image/*" id="file" hidden>
      <button id="pick">${l.has_photo ? "更換照片" : "上傳照片"}</button>
      <div class="msg" id="msg"></div>`;
    const file = document.getElementById("file"), pick = document.getElementById("pick"),
          msg = document.getElementById("msg");
    pick.onclick = () => file.click();
    file.onchange = async () => {
      const f = file.files[0]; if (!f) return;
      pick.disabled = true; msg.textContent = "上傳中…";
      try {
        const dataUrl = await shrink(f);
        await callTool("app_upload_photo", { upload_token: d.upload_token, image_base64: dataUrl });
        ph.innerHTML = `<img alt="商品照片" src="${dataUrl}">`;
        msg.textContent = "照片已上傳！"; pick.textContent = "更換照片";
      } catch (e) { msg.textContent = "上傳失敗：" + e.message; }
      pick.disabled = false;
    };
  }

  function render(d) {
    if (!d) return;
    if (d.view === "gallery") renderGallery(d);
    else if (d.view === "listing") renderListing(d);
    else { root.className = "empty"; root.textContent = d.error || "沒有可顯示的內容"; }
  }
})();
</script>
</body></html>
"""

apps = Apps()
apps.add_html_resource(
    APP_URI,
    APP_HTML,
    name="DealMate",
    title="DealMate 商品卡片",
    description="Product cards with photos, and in-chat photo upload for sellers.",
    prefers_border=True,
)
