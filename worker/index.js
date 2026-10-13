// 配信後に見つけた場面の一覧ページ (Cloudflare Workers)。
//
// - AX-03 などで動く twitch-shorts が、配信後に場面の一覧を POST /api/upload に送る (UPLOAD_TOKEN で認証)
// - ページは Twitch でログインした ALLOWED_LOGIN のアカウントだけが見られる
// - 各場面の「クリップを作る」で、その配信の VOD から Twitch クリップを作る (Create Clip From VOD)
//
// 必要な設定 (Cloudflare のダッシュボード → Workers → aiw → 設定 → 変数とシークレット):
//   TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET : Twitch Developer Console のアプリ
//   UPLOAD_TOKEN   : twitch-shorts の config.toml [web] upload_token と同じ長いランダムな文字列
//   SESSION_SECRET : ログイン状態の署名用の長いランダムな文字列
//   ALLOWED_LOGIN  : ページを使える Twitch のログイン名 (wrangler.toml で yuuki_ftw)

const HELIX = "https://api.twitch.tv/helix";
const OAUTH = "https://id.twitch.tv/oauth2";
const SCOPES = "channel:manage:clips clips:edit";
const SESSION_DAYS = 30;
const MAX_RUNS = 50;
const enc = new TextEncoder();

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (e) {
      console.error(e);
      return text("エラーが発生しました: " + (e && e.message ? e.message : e), 500);
    }
  },
};

async function route(request, env) {
  const url = new URL(request.url);
  const missing = ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "UPLOAD_TOKEN", "SESSION_SECRET"]
    .filter((k) => !env[k]);
  if (missing.length) {
    return text("設定が足りません: " + missing.join(", ") + " を Cloudflare のシークレットに登録してください", 500);
  }
  if (url.pathname === "/api/upload" && request.method === "POST") return upload(request, env);
  if (url.pathname === "/login") return login(url, env);
  if (url.pathname === "/callback") return callback(request, url, env);
  if (url.pathname === "/logout") {
    return new Response(null, { status: 302, headers: { Location: "/", "Set-Cookie": cookie("session", "", 0) } });
  }
  const user = await currentUser(request, env);
  if (url.pathname === "/api/clip" && request.method === "POST") {
    if (!user) return json({ error: "ログインしてください" }, 401);
    if (!sameOrigin(request, url)) return json({ error: "不正なリクエストです" }, 403);
    return createClip(request, env);
  }
  if (url.pathname === "/" && request.method === "GET") return page(env, user);
  return text("見つかりません", 404);
}

// ---------------------------------------------------------------------------
// 一覧の受け取り (twitch-shorts から)
// ---------------------------------------------------------------------------

async function upload(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (!safeEqual(auth, "Bearer " + env.UPLOAD_TOKEN)) return json({ error: "認証に失敗しました" }, 401);
  let run;
  try {
    run = await request.json();
  } catch {
    return json({ error: "JSON ではありません" }, 400);
  }
  if (!run || typeof run.id !== "string" || !/^[0-9A-Za-z_-]{1,120}$/.test(run.id) ||
      typeof run.vod_id !== "string" || !/^[0-9]{1,20}$/.test(run.vod_id) || !Array.isArray(run.items)) {
    return json({ error: "形式が違います" }, 400);
  }
  const old = await getJSON(env, "run:" + run.id);
  const items = run.items.slice(0, 50).map((it, i) => {
    // 送り直しても、ページで作ったクリップは消さない
    const prev = old && old.items && old.items[i];
    return {
      start: num(it.start), end: num(it.end), score: num(it.score),
      title: str(it.title, 100), hook: str(it.hook, 40), category: str(it.category, 20), reason: str(it.reason, 200),
      short: !!it.short, clip_end: Math.ceil(num(it.clip_end)), clip_duration: num(it.clip_duration),
      twitch_clip: str(it.twitch_clip, 300) || (prev && prev.start === num(it.start) ? prev.twitch_clip || "" : ""),
    };
  });
  const record = {
    id: run.id, channel: str(run.channel, 50), stream_title: str(run.stream_title, 200), vod_id: run.vod_id,
    uploaded_at: new Date().toISOString(), items,
  };
  await env.KV.put("run:" + run.id, JSON.stringify(record));
  const index = ((await getJSON(env, "runs")) || []).filter((id) => id !== run.id);
  index.unshift(run.id);
  const removed = index.splice(MAX_RUNS);
  await env.KV.put("runs", JSON.stringify(index));
  for (const id of removed) await env.KV.delete("run:" + id);
  return json({ ok: true, items: items.length });
}

// ---------------------------------------------------------------------------
// Twitch でログイン
// ---------------------------------------------------------------------------

function redirectUri(url) {
  return url.origin + "/callback";
}

function login(url, env) {
  const state = randomToken();
  const auth = new URL(OAUTH + "/authorize");
  auth.search = new URLSearchParams({
    response_type: "code", client_id: env.TWITCH_CLIENT_ID, redirect_uri: redirectUri(url), scope: SCOPES, state,
  }).toString();
  return new Response(null, {
    status: 302,
    headers: { Location: auth.toString(), "Set-Cookie": cookie("oauth_state", state, 600) },
  });
}

async function callback(request, url, env) {
  const state = url.searchParams.get("state") || "";
  const expected = readCookie(request, "oauth_state");
  if (!expected || !safeEqual(state, expected)) return text("ログインをやり直してください (state が一致しません)", 400);
  const code = url.searchParams.get("code");
  if (!code) return text("ログインがキャンセルされました", 400);
  const r = await fetch(OAUTH + "/token", {
    method: "POST",
    body: new URLSearchParams({
      client_id: env.TWITCH_CLIENT_ID, client_secret: env.TWITCH_CLIENT_SECRET, code,
      grant_type: "authorization_code", redirect_uri: redirectUri(url),
    }),
  });
  if (!r.ok) return text("Twitch からトークンを受け取れませんでした: " + r.status, 502);
  const tok = await r.json();
  const info = await validate(tok.access_token);
  if (!info) return text("Twitch のログイン情報を確認できませんでした", 502);
  const allowed = (env.ALLOWED_LOGIN || "").toLowerCase();
  if (!allowed || info.login.toLowerCase() !== allowed) {
    return text("このページは " + (allowed || "(未設定)") + " のアカウントだけが使えます", 403);
  }
  await env.KV.put("token", JSON.stringify({
    access_token: tok.access_token, refresh_token: tok.refresh_token,
    expires_at: Date.now() + (tok.expires_in || 3600) * 1000, user_id: info.user_id, login: info.login,
  }));
  const exp = Date.now() + SESSION_DAYS * 86400 * 1000;
  const value = info.login + "." + exp;
  const session = value + "." + (await hmac(env.SESSION_SECRET, value));
  const headers = new Headers({ Location: "/" });
  headers.append("Set-Cookie", cookie("session", session, SESSION_DAYS * 86400));
  headers.append("Set-Cookie", cookie("oauth_state", "", 0));
  return new Response(null, { status: 302, headers });
}

async function validate(accessToken) {
  const r = await fetch(OAUTH + "/validate", { headers: { Authorization: "OAuth " + accessToken } });
  if (!r.ok) return null;
  const v = await r.json();
  return v && v.login && v.user_id ? v : null;
}

async function currentUser(request, env) {
  const raw = readCookie(request, "session");
  if (!raw) return null;
  const parts = raw.split(".");
  if (parts.length !== 3) return null;
  const [login, exp, sig] = parts;
  if (!safeEqual(sig, await hmac(env.SESSION_SECRET, login + "." + exp))) return null;
  if (Number(exp) < Date.now()) return null;
  if (login.toLowerCase() !== (env.ALLOWED_LOGIN || "").toLowerCase()) return null;
  return login;
}

async function accessToken(env, forceRefresh = false) {
  const tok = await getJSON(env, "token");
  if (!tok) throw new Error("Twitch のログイン情報がありません。ログインし直してください");
  if (!forceRefresh && tok.expires_at > Date.now() + 60000) return tok;
  const r = await fetch(OAUTH + "/token", {
    method: "POST",
    body: new URLSearchParams({
      client_id: env.TWITCH_CLIENT_ID, client_secret: env.TWITCH_CLIENT_SECRET,
      grant_type: "refresh_token", refresh_token: tok.refresh_token,
    }),
  });
  if (!r.ok) throw new Error("Twitch のログインの更新に失敗しました。ログインし直してください");
  const n = await r.json();
  const updated = {
    ...tok, access_token: n.access_token, refresh_token: n.refresh_token || tok.refresh_token,
    expires_at: Date.now() + (n.expires_in || 3600) * 1000,
  };
  await env.KV.put("token", JSON.stringify(updated));
  return updated;
}

// ---------------------------------------------------------------------------
// クリップを作る (Create Clip From VOD)
// ---------------------------------------------------------------------------

async function createClip(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "JSON ではありません" }, 400);
  }
  const run = await getJSON(env, "run:" + String(body.run || ""));
  const index = Number(body.index);
  const item = run && Number.isInteger(index) ? run.items[index] : null;
  if (!item) return json({ error: "場面が見つかりません" }, 404);
  if (item.twitch_clip) return json({ url: item.twitch_clip, existing: true });

  let tok = await accessToken(env);
  const params = new URLSearchParams({
    broadcaster_id: tok.user_id, editor_id: tok.user_id, vod_id: run.vod_id,
    vod_offset: String(Math.max(item.clip_end, Math.ceil(item.clip_duration))),
    duration: String(item.clip_duration),
  });
  if (item.title) params.set("title", item.title.slice(0, 100));
  let r;
  for (let attempt = 0; attempt < 3; attempt++) {
    r = await fetch(HELIX + "/videos/clips?" + params.toString(), {
      method: "POST",
      headers: { "Client-Id": env.TWITCH_CLIENT_ID, Authorization: "Bearer " + tok.access_token },
    });
    if (r.status === 401 && attempt === 0) {
      tok = await accessToken(env, true);
      continue;
    }
    if (r.status === 400 && params.has("title") && (await r.clone().text()).toLowerCase().includes("title")) {
      params.delete("title"); // タイトルを受け付けない場合はタイトル無しで作る
      continue;
    }
    break;
  }
  if (r.status === 403) return json({ error: "クリップを作る権限がありません (ログインし直してください)" }, 403);
  if (r.status !== 200 && r.status !== 202) {
    return json({ error: "クリップを作れませんでした: " + r.status + " " + (await r.text()).slice(0, 200) }, 502);
  }
  const data = ((await r.json()).data || [])[0];
  if (!data) return json({ error: "クリップ作成の応答が空でした" }, 502);
  const clipUrl = data.id ? "https://clips.twitch.tv/" + data.id : data.edit_url || "";
  // 同時に別のボタンが押されても消えないよう、読み直してから書く
  const latest = (await getJSON(env, "run:" + run.id)) || run;
  if (latest.items[index]) latest.items[index].twitch_clip = clipUrl;
  await env.KV.put("run:" + run.id, JSON.stringify(latest));
  return json({ url: clipUrl, edit_url: data.edit_url || "" });
}

// ---------------------------------------------------------------------------
// ページ
// ---------------------------------------------------------------------------

async function page(env, user) {
  if (!user) {
    return html(layout(`
      <h1>切り抜き候補</h1>
      <p class="muted">配信後に自動で見つけた場面の一覧です。配信者のアカウントでログインしてください。</p>
      <a class="btn" href="/login">Twitch でログイン</a>`));
  }
  const ids = (await getJSON(env, "runs")) || [];
  const runs = [];
  for (const id of ids.slice(0, 20)) {
    const run = await getJSON(env, "run:" + id);
    if (run) runs.push(run);
  }
  // JSON は <script> の中に置く。"<" を逃がして、文字列の中から </script> で抜け出せないようにする
  const data = JSON.stringify(runs).replace(/</g, "\\u003c");
  return html(layout(`
    <header><h1>切り抜き候補</h1><span class="muted">${escapeHtml(user)} でログイン中 · <a href="/logout">ログアウト</a></span></header>
    <p class="muted">「クリップを作る」で、その場面を配信の VOD から Twitch クリップにします。★ はショートにした場面です。</p>
    <div id="runs"></div>
    <script id="data" type="application/json">${data}</script>
    <script>${CLIENT_JS}</script>`));
}

const CLIENT_JS = `
const runs = JSON.parse(document.getElementById("data").textContent);
const root = document.getElementById("runs");
const hms = (t) => { t = Math.max(0, Math.floor(t)); const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
  return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(s).padStart(2, "0"); };
const vodTime = (t) => { t = Math.max(0, Math.floor(t)); return Math.floor(t / 3600) + "h" + Math.floor(t % 3600 / 60) + "m" + (t % 60) + "s"; };
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function clipLink(url) { const a = el("a", "clip", "クリップを見る"); a.href = url; a.target = "_blank"; a.rel = "noopener"; return a; }
if (!runs.length) root.append(el("p", "muted", "まだ一覧がありません。配信が終わると twitch-shorts から送られてきます。"));
for (const run of runs) {
  const sec = el("section", "run");
  const head = el("div", "run-head");
  head.append(el("h2", null, run.stream_title || run.channel || "配信"));
  head.append(el("span", "muted", new Date(run.uploaded_at).toLocaleString("ja-JP") + " · VOD " + run.vod_id));
  sec.append(head);
  run.items.forEach((it, i) => {
    const row = el("div", "item" + (it.short ? " short" : ""));
    const time = el("a", "time", hms(it.start) + "〜" + hms(it.end));
    time.href = "https://www.twitch.tv/videos/" + run.vod_id + "?t=" + vodTime(it.start); time.target = "_blank"; time.rel = "noopener";
    const body = el("div", "body");
    body.append(el("div", "title", (it.short ? "★ " : "") + it.title));
    const meta = [it.hook && "「" + it.hook + "」", it.category, "スコア " + it.score].filter(Boolean).join(" · ");
    body.append(el("div", "muted small", meta));
    if (it.reason) body.append(el("div", "muted small", it.reason));
    const action = el("div", "action");
    if (it.twitch_clip) action.append(clipLink(it.twitch_clip));
    else {
      const btn = el("button", "btn", "クリップを作る");
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = "作成中…";
        try {
          const r = await fetch("/api/clip", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ run: run.id, index: i }) });
          const res = await r.json();
          if (!r.ok) throw new Error(res.error || r.status);
          action.replaceChildren(clipLink(res.url));
        } catch (e) { btn.disabled = false; btn.textContent = "クリップを作る"; alert(e.message); }
      };
      action.append(btn);
    }
    row.append(time, body, action);
    sec.append(row);
  });
  root.append(sec);
}
`;

function layout(body) {
  return `<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>切り抜き候補</title>
<style>
:root{--bg:#fafafa;--fg:#18181b;--muted:#71717a;--card:#fff;--line:#e4e4e7;--accent:#9146ff;--star:#fff7e6}
@media (prefers-color-scheme: dark){:root{--bg:#0e0e10;--fg:#efeff1;--muted:#a1a1aa;--card:#18181b;--line:#2f2f35;--star:#2a2416}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;justify-content:space-between}
h1{font-size:22px;margin:0 0 8px}h2{font-size:17px;margin:0}
a{color:var(--accent)}.muted{color:var(--muted)}.small{font-size:13px}
.btn{display:inline-block;background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:disabled{opacity:.6;cursor:default}
.run{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:16px 0;overflow:hidden}
.run-head{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:2px}
.item{display:grid;grid-template-columns:110px 1fr auto;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
.item:last-child{border-bottom:0}.item.short{background:var(--star)}
.time{font-variant-numeric:tabular-nums;font-size:14px}.title{font-weight:600}
.clip{white-space:nowrap}
@media (max-width:560px){.item{grid-template-columns:1fr;gap:4px}}
</style></head><body><main>${body}</main></body></html>`;
}

// ---------------------------------------------------------------------------
// 小物
// ---------------------------------------------------------------------------

function text(body, status = 200) {
  return new Response(body, { status, headers: { "Content-Type": "text/plain; charset=utf-8" } });
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json; charset=utf-8" } });
}

function html(body) {
  return new Response(body, {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "same-origin",
    },
  });
}

function cookie(name, value, maxAge) {
  return `${name}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;
}

function readCookie(request, name) {
  const header = request.headers.get("Cookie") || "";
  for (const part of header.split(/;\s*/)) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i) === name) return part.slice(i + 1);
  }
  return "";
}

function sameOrigin(request, url) {
  const origin = request.headers.get("Origin");
  return origin === url.origin;
}

async function getJSON(env, key) {
  const raw = await env.KV.get(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

async function hmac(secret, data) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", key, enc.encode(data)));
}

function b64url(buf) {
  let s = "";
  for (const b of new Uint8Array(buf)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomToken() {
  const b = new Uint8Array(24);
  crypto.getRandomValues(b);
  return b64url(b);
}

function safeEqual(a, b) {
  a = String(a);
  b = String(b);
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function str(v, max) {
  return typeof v === "string" ? v.slice(0, max) : "";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
