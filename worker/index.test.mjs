// worker/index.js のテスト。node --test worker/ で実行する (Twitch と KV は模擬)。
import assert from "node:assert/strict";
import { test, beforeEach } from "node:test";
import worker from "./index.js";

const ORIGIN = "https://aiw.example.workers.dev";
let kv;
let calls;
let twitchLogin;

function makeEnv() {
  return {
    TWITCH_CLIENT_ID: "cid", TWITCH_CLIENT_SECRET: "secret", UPLOAD_TOKEN: "up-token",
    SESSION_SECRET: "session-secret", ALLOWED_LOGIN: "yuuki_ftw",
    KV: {
      get: async (k) => (kv.has(k) ? kv.get(k).value : null),
      put: async (k, v, opts = {}) => void kv.set(k, { value: v, metadata: opts.metadata }),
      delete: async (k) => void kv.delete(k),
      list: async ({ prefix }) => ({
        keys: [...kv.entries()].filter(([k]) => k.startsWith(prefix)).map(([name, e]) => ({ name, metadata: e.metadata })),
        list_complete: true,
      }),
    },
  };
}

beforeEach(() => {
  kv = new Map();
  calls = [];
  twitchLogin = "yuuki_ftw";
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.startsWith("https://id.twitch.tv/oauth2/token")) {
      return Response.json({ access_token: "at", refresh_token: "rt", expires_in: 3600 });
    }
    if (url.startsWith("https://id.twitch.tv/oauth2/validate")) {
      return Response.json({ login: twitchLogin, user_id: "42", scopes: ["channel:manage:clips"] });
    }
    if (url.startsWith("https://api.twitch.tv/helix/videos/clips")) {
      return Response.json({ data: [{ id: "ClipABC", edit_url: "https://clips.twitch.tv/ClipABC/edit" }] }, { status: 202 });
    }
    throw new Error("unexpected fetch " + url);
  };
});

const req = (path, init = {}) => new Request(ORIGIN + path, init);
const run = { id: "123-20261013_200000", channel: "yuuki_ftw", stream_title: "配信", vod_id: "123",
  items: [{ start: 100, end: 130, score: 2.5, title: "神プレイ", hook: "まさかの", short: true, clip_end: 130, clip_duration: 30 },
          { start: 400, end: 420, score: 1.2, title: "<script>alert(1)</script>", short: false, clip_end: 420, clip_duration: 20 }] };

async function upload(env, body = run, token = "up-token") {
  return worker.fetch(req("/api/upload", { method: "POST", headers: { Authorization: "Bearer " + token },
    body: JSON.stringify(body) }), env);
}

async function loginCookie(env) {
  const start = await worker.fetch(req("/login"), env);
  assert.equal(start.status, 302);
  const state = new URL(start.headers.get("Location")).searchParams.get("state");
  const cb = await worker.fetch(req(`/callback?code=abc&state=${state}`, { headers: { Cookie: `oauth_state=${state}` } }), env);
  return cb;
}

function sessionFrom(res) {
  const all = res.headers.getSetCookie ? res.headers.getSetCookie() : [res.headers.get("Set-Cookie")];
  const c = all.find((v) => v.startsWith("session=") && !v.startsWith("session=;"));
  return c ? c.split(";")[0] : "";
}

test("upload requires the token and stores the run", async () => {
  const env = makeEnv();
  assert.equal((await upload(env, run, "wrong")).status, 401);
  assert.equal((await upload(env, { ...run, vod_id: "abc" })).status, 400);
  const res = await upload(env);
  assert.equal(res.status, 200);
  assert.equal(JSON.parse(kv.get("run:" + run.id).value).items.length, 2);
});

test("page shows login button until the allowed user logs in", async () => {
  const env = makeEnv();
  await upload(env);
  const anon = await (await worker.fetch(req("/"), env)).text();
  assert.match(anon, /Twitch でログイン/);
  assert.doesNotMatch(anon, /神プレイ/);

  const cb = await loginCookie(env);
  assert.equal(cb.status, 302);
  const cookie = sessionFrom(cb);
  assert.ok(cookie);
  const page = await (await worker.fetch(req("/", { headers: { Cookie: cookie } }), env)).text();
  assert.match(page, /神プレイ/);
  // 一覧のデータから </script> で抜け出せない
  assert.doesNotMatch(page, /<script>alert\(1\)<\/script>/);
});

test("other Twitch accounts are rejected", async () => {
  const env = makeEnv();
  twitchLogin = "someone_else";
  const cb = await loginCookie(env);
  assert.equal(cb.status, 403);
  assert.equal(sessionFrom(cb), "");
  assert.equal(kv.has("token"), false);
});

test("forged or wrong-state requests are rejected", async () => {
  const env = makeEnv();
  const bad = await worker.fetch(req("/callback?code=abc&state=x", { headers: { Cookie: "oauth_state=y" } }), env);
  assert.equal(bad.status, 400);
  const forged = await worker.fetch(req("/", { headers: { Cookie: "session=yuuki_ftw.99999999999999.AAAA" } }), env);
  assert.match(await forged.text(), /Twitch でログイン/);
});

test("clip button creates a clip from the VOD and remembers it", async () => {
  const env = makeEnv();
  await upload(env);
  const cookie = sessionFrom(await loginCookie(env));
  const post = (headers) => worker.fetch(req("/api/clip", { method: "POST",
    headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify({ run: run.id, start: 100 }) }), env);

  assert.equal((await post({ Origin: ORIGIN })).status, 401); // 未ログイン
  assert.equal((await post({ Cookie: cookie, Origin: "https://evil.example" })).status, 403); // 別サイトから

  const res = await post({ Cookie: cookie, Origin: ORIGIN });
  assert.equal(res.status, 200);
  assert.equal((await res.json()).url, "https://clips.twitch.tv/ClipABC");
  const helix = calls.find((c) => c.url.includes("/helix/videos/clips"));
  const q = new URL(helix.url).searchParams;
  assert.equal(q.get("vod_id"), "123");
  assert.equal(q.get("vod_offset"), "130");
  assert.equal(q.get("duration"), "30");
  assert.equal(q.get("broadcaster_id"), "42");
  assert.equal(JSON.parse(kv.get("run:" + run.id).value).items[0].twitch_clip, "https://clips.twitch.tv/ClipABC");

  // 2 回目は作らずに同じクリップを返す
  const before = calls.length;
  assert.equal((await (await post({ Cookie: cookie, Origin: ORIGIN })).json()).existing, true);
  assert.equal(calls.length, before);

  // 送り直して並び順が変わっても、ページで作ったクリップは同じ場面に残る
  const reordered = { ...run, items: [{ start: 50, end: 70, score: 1, title: "新しい場面", clip_end: 70, clip_duration: 20 },
    ...run.items] };
  await upload(env, reordered);
  const items = JSON.parse(kv.get("run:" + run.id).value).items;
  assert.equal(items[0].twitch_clip, "");
  assert.equal(items[1].twitch_clip, "https://clips.twitch.tv/ClipABC");
});

test("only Twitch clip URLs are accepted from uploads", async () => {
  const env = makeEnv();
  await upload(env, { ...run, items: [{ ...run.items[0], twitch_clip: "javascript:alert(1)" }] });
  assert.equal(JSON.parse(kv.get("run:" + run.id).value).items[0].twitch_clip, "");
  assert.equal((await upload(env, { ...run, items: [null] })).status, 400);
});

test("logout needs a same-site POST and ends every session", async () => {
  const env = makeEnv();
  const cookie = sessionFrom(await loginCookie(env));
  const loggedIn = async () => !/Twitch でログイン/.test(await (await worker.fetch(req("/", { headers: { Cookie: cookie } }), env)).text());
  assert.equal(await loggedIn(), true);
  assert.equal((await worker.fetch(req("/logout"), env)).status, 404); // GET では何もしない
  assert.equal((await worker.fetch(req("/logout", { method: "POST", headers: { Cookie: cookie, Origin: "https://evil.example" } }), env)).status, 403);
  assert.equal(await loggedIn(), true);
  const out = await worker.fetch(req("/logout", { method: "POST", headers: { Cookie: cookie, Origin: ORIGIN } }), env);
  assert.equal(out.status, 303);
  assert.equal(await loggedIn(), false); // 残っていた cookie も使えない
});

test("keeps only the newest runs", async () => {
  const env = makeEnv();
  for (let i = 0; i < 52; i++) {
    await upload(env, { ...run, id: "r" + i });
    await new Promise((ok) => setTimeout(ok, 2));
  }
  const names = [...kv.keys()].filter((k) => k.startsWith("run:"));
  assert.equal(names.length, 50);
  assert.ok(!names.includes("run:r0") && names.includes("run:r51"));
});

test("missing secrets are reported", async () => {
  const env = makeEnv();
  delete env.SESSION_SECRET;
  const res = await worker.fetch(req("/"), env);
  assert.equal(res.status, 500);
  assert.match(await res.text(), /SESSION_SECRET/);
});
