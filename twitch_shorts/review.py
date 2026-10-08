"""作ったショートを見比べて選ぶための確認ページ (output/<実行>/index.html) を作る。

参考: KumoMoments のタイムライン表示 (スコア・カテゴリで絞り込み、場面ごとに確認してから使う)。
外部ファイルやサーバーは不要で、ブラウザでそのまま開ける。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np

from .models import Highlight

MAX_POINTS = 600  # タイムラインに描く点の数 (長い配信は間引く)


def _downsample(score: np.ndarray) -> list[float]:
    if score is None or len(score) == 0:
        return []
    if len(score) <= MAX_POINTS:
        return [round(float(v), 2) for v in score]
    edges = np.linspace(0, len(score), MAX_POINTS + 1).astype(int)
    return [round(float(score[a:b].max()), 2) for a, b in zip(edges[:-1], edges[1:])]


def write_review_page(run_dir: Path, highlights: list[Highlight], score: np.ndarray | None,
                      duration: float, channel: str, stream_title: str) -> Path:
    items = []
    for h in sorted(highlights, key=lambda h: h.start):
        video = ""
        if h.output_path:
            p = Path(h.output_path)
            video = p.name if p.parent.resolve() == run_dir.resolve() else p.resolve().as_uri()
        items.append({
            "title": h.title, "hook": h.hook, "category": h.category or "未分類", "score": h.score,
            "start": h.start, "end": h.end, "peak": h.peak, "signals": h.signals, "reason": h.reason,
            "description": h.description, "hashtags": h.hashtags, "chat": h.chat_sample[:12],
            "video": video, "twitch_clip": h.twitch_clip, "twitch_clip_edit": h.twitch_clip_edit,
            "vod_url": h.vod_url,
        })
    data = {"channel": channel, "title": stream_title, "duration": duration,
            "score": _downsample(score), "items": items}
    # チャットや AI の文字列に "<!--" や "</script>" があってもページが壊れないよう、< > & をエスケープする
    payload = (json.dumps(data, ensure_ascii=False)
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    page = TEMPLATE.replace("__TITLE__", html.escape(f"{channel or '配信'} の切り抜き候補")).replace("__DATA__", payload)
    out = run_dir / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f6f6f8; --card: #ffffff; --text: #18181b; --muted: #6b6b76; --line: #e4e4ea;
  --accent: #7c3aed; --accent-soft: #ede9fe; --mark: #f59e0b;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #111114; --card: #1c1c21; --text: #ececf1; --muted: #9a9aa6; --line: #2e2e36;
          --accent: #a78bfa; --accent-soft: #2e2546; --mark: #fbbf24; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font-family: system-ui, "Hiragino Sans", "Yu Gothic UI", "Noto Sans CJK JP", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 20px; font-size: .9rem; }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
svg { width: 100%; height: 120px; display: block; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.chip { border: 1px solid var(--line); background: transparent; color: var(--text); padding: 6px 12px;
        border-radius: 999px; cursor: pointer; font-size: .85rem; }
.chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
label { color: var(--muted); font-size: .85rem; margin-left: auto; }
input[type=range] { accent-color: var(--accent); vertical-align: middle; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
        display: flex; flex-direction: column; }
.card video { width: 100%; aspect-ratio: 9 / 16; background: #000; display: block; }
.body { padding: 12px; display: flex; flex-direction: column; gap: 6px; flex: 1; }
.meta { display: flex; gap: 6px; flex-wrap: wrap; font-size: .75rem; }
.badge { background: var(--accent-soft); color: var(--accent); padding: 2px 8px; border-radius: 999px; }
.time { color: var(--muted); }
.title { font-weight: 700; line-height: 1.4; }
.hook { color: var(--mark); font-weight: 700; font-size: .9rem; }
.desc { font-size: .85rem; color: var(--muted); }
.tags { font-size: .8rem; color: var(--accent); word-break: break-all; }
details { font-size: .8rem; color: var(--muted); }
.actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: auto; padding-top: 6px; }
.actions a, .actions button { font-size: .8rem; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--line);
  background: transparent; color: var(--text); text-decoration: none; cursor: pointer; }
.empty { color: var(--muted); text-align: center; padding: 32px; }
</style>
</head>
<body>
<main>
  <h1 id="h"></h1>
  <p class="sub" id="sub"></p>
  <section class="panel">
    <svg id="timeline" viewBox="0 0 1000 120" preserveAspectRatio="none" role="img" aria-label="盛り上がりスコアの推移"></svg>
  </section>
  <section class="panel filters" id="filters"></section>
  <section class="grid" id="grid"></section>
</main>
<script>
const DATA = __DATA__;
const fmt = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
document.getElementById("h").textContent = (DATA.channel || "配信") + " の切り抜き候補";
document.getElementById("sub").textContent = (DATA.title ? DATA.title + " ・ " : "") + DATA.items.length + " 本 ・ 配信 " + fmt(DATA.duration);

// タイムライン: 盛り上がりスコアの推移と、ショートにした区間
(function () {
  const svg = document.getElementById("timeline"), pts = DATA.score, W = 1000, H = 120;
  if (pts.length < 2 || !(DATA.duration > 0)) return;
  const max = Math.max(1, ...pts), min = Math.min(0, ...pts);
  const y = v => H - 8 - (v - min) / (max - min) * (H - 16);
  const css = getComputedStyle(document.documentElement);
  let d = pts.map((v, i) => (i ? "L" : "M") + (i / (pts.length - 1) * W).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
  DATA.items.forEach(it => {
    const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", it.start / DATA.duration * W); r.setAttribute("y", 0);
    r.setAttribute("width", Math.max(2, (it.end - it.start) / DATA.duration * W)); r.setAttribute("height", H);
    r.setAttribute("fill", css.getPropertyValue("--accent-soft"));
    svg.appendChild(r);
  });
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", d); p.setAttribute("fill", "none"); p.setAttribute("stroke", css.getPropertyValue("--accent"));
  p.setAttribute("stroke-width", "2"); p.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(p);
})();

// 絞り込み: カテゴリとスコア
let cat = "すべて", minScore = 0;
const cats = ["すべて", ...new Set(DATA.items.map(i => i.category))];
const filters = document.getElementById("filters");
cats.forEach(c => { const b = el("button", "chip" + (c === cat ? " on" : ""), c);
  b.onclick = () => { cat = c; [...filters.querySelectorAll(".chip")].forEach(x => x.classList.toggle("on", x === b)); render(); };
  filters.appendChild(b); });
const lab = el("label"); lab.textContent = "スコア ";
const range = document.createElement("input"); range.type = "range"; range.min = 0;
range.max = Math.ceil(Math.max(1, ...DATA.items.map(i => i.score))); range.step = 0.5; range.value = 0;
const rv = el("span", null, "0 以上"); range.oninput = () => { minScore = +range.value; rv.textContent = range.value + " 以上"; render(); };
lab.append(range, " ", rv); filters.appendChild(lab);

function copy(text, btn) {
  navigator.clipboard.writeText(text).then(() => { const t = btn.textContent; btn.textContent = "コピーしました"; setTimeout(() => btn.textContent = t, 1200); });
}
function render() {
  const grid = document.getElementById("grid"); grid.replaceChildren();
  const list = DATA.items.filter(i => (cat === "すべて" || i.category === cat) && i.score >= minScore)
                         .sort((a, b) => b.score - a.score);
  if (!list.length) { grid.appendChild(el("p", "empty", "条件に合う候補がありません")); return; }
  list.forEach(it => {
    const c = el("article", "card");
    if (it.video) { const v = document.createElement("video"); v.src = it.video; v.controls = true; v.preload = "metadata"; c.appendChild(v); }
    const b = el("div", "body");
    const meta = el("div", "meta");
    meta.append(el("span", "badge", it.category), el("span", "badge", "スコア " + it.score.toFixed(1)),
                el("span", "time", fmt(it.start) + "〜" + fmt(it.end)));
    b.append(meta, el("div", "title", it.title));
    if (it.hook) b.appendChild(el("div", "hook", "「" + it.hook + "」"));
    if (it.description) b.appendChild(el("div", "desc", it.description));
    if (it.hashtags.length) b.appendChild(el("div", "tags", it.hashtags.join(" ")));
    const det = el("details"); det.appendChild(el("summary", null, "根拠 (シグナル・チャット)"));
    det.appendChild(el("div", null, Object.entries(it.signals).map(([k, v]) => k + " " + v).join(" / ")));
    if (it.reason) det.appendChild(el("div", null, it.reason));
    if (it.chat.length) det.appendChild(el("div", null, it.chat.join(" ・ ")));
    b.appendChild(det);
    const act = el("div", "actions");
    const cp = el("button", null, "タイトル+タグをコピー");
    cp.onclick = () => copy([it.title, it.description, it.hashtags.join(" ")].filter(Boolean).join("\\n"), cp);
    act.appendChild(cp);
    if (it.twitch_clip) { const a = el("a", null, "Twitch クリップ"); a.href = it.twitch_clip; a.target = "_blank"; act.appendChild(a); }
    if (it.twitch_clip_edit) { const a = el("a", null, "クリップを編集"); a.href = it.twitch_clip_edit; a.target = "_blank"; act.appendChild(a); }
    if (it.vod_url) { const a = el("a", null, "VOD で開く"); a.href = it.vod_url; a.target = "_blank"; act.appendChild(a); }
    b.appendChild(act); c.appendChild(b); grid.appendChild(c);
  });
}
render();
</script>
</body>
</html>
"""
