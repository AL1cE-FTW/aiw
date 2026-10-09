"""コマンドラインインターフェース。

  twitch-shorts vod <VODのURL/ID>        過去配信(VOD)からショート動画を作る
  twitch-shorts latest <チャンネル>       チャンネルの最新アーカイブを処理 (処理済みはスキップ。cron 向け)
  twitch-shorts auto <チャンネル>         【おすすめ】起動しておくだけで、配信ごとに自動でショートとクリップを作る
  twitch-shorts watch <チャンネル>        配信を監視して録画し、自動でショート動画を作る
  twitch-shorts login                   配信者アカウントで Twitch にログイン (公式クリップの自動作成用)
  twitch-shorts doctor [チャンネル]       必要なものが揃っているか確認する
  twitch-shorts local <動画> --chat <ファイル>  手元の録画ファイルから作る
  twitch-shorts chat <VODのURL/ID>       VOD のチャットを JSONL で保存する
  twitch-shorts analyze <チャンネル>      人気クリップを分析し、レポートと推奨設定を作る
  twitch-shorts schedule                作ったショートの投稿予定 (毎日決まった時刻に 1 本) を表示
  twitch-shorts feedback <CSV>          YouTube Studio の数値を取り込み、結果から検出設定を調整する
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config, load_config
from .fileutil import add_processed_vod, processed_vods

log = logging.getLogger("twitch_shorts")


def _common_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if getattr(args, "output", None):
        cfg.output_dir = args.output
    if getattr(args, "top", None):
        cfg.detect.top_n = args.top
    if getattr(args, "layout", None):
        cfg.render.layout = args.layout
    if getattr(args, "llm", False):
        cfg.llm.enabled = True
    if getattr(args, "transcribe", False):
        cfg.transcribe.enabled = True
    if getattr(args, "no_subtitles", False):
        cfg.render.subtitles = False
    if getattr(args, "twitch_clips", False):
        cfg.clips.enabled = True


def _print_result(highlights) -> None:
    if not highlights:
        print("ハイライトは見つかりませんでした (detect.min_score を下げると検出されやすくなります)")
        return
    for h in highlights:
        where = h.output_path or "(dry-run)"
        print(f"[{h.score:5.2f}] {int(h.start)//60:>4}:{int(h.start)%60:02d}〜{int(h.end)//60}:{int(h.end)%60:02d}  {h.title}  -> {where}")


def cmd_vod(cfg: Config, args: argparse.Namespace) -> int:
    from .chat import fetch_vod_chat, load_chat, save_chat_jsonl
    from .download import vod_url
    from .pipeline import VodSource, process
    from .twitch_api import HelixClient, parse_video_id

    video_id = parse_video_id(args.vod)
    url = vod_url(video_id)
    work = Path(cfg.work_dir) / f"vod_{video_id}"
    work.mkdir(parents=True, exist_ok=True)

    channel, title, duration, clips = args.channel or "", "", None, []
    owner = ""  # VOD の持ち主 (クリップを作れるかの判定用)。API で確認できたときだけ分かる
    if cfg.twitch.client_id and cfg.twitch.client_secret:
        helix = HelixClient(cfg.twitch.client_id, cfg.twitch.client_secret)
        info = helix.get_video(video_id)
        channel, title, duration = channel or info.user_login, info.title, info.duration
        owner = info.user_login
        if not args.no_clips:
            clips = helix.get_clips_for_video(info)
            log.info("既存クリップ %d 件をシグナルとして使用", len(clips))
    else:
        log.info("TWITCH_CLIENT_ID 未設定のため、既存クリップのシグナルは使いません")

    if args.chat:
        chat = load_chat(args.chat)
    else:
        cache = work / "chat.jsonl"
        if cache.exists():
            chat = load_chat(cache)
        else:
            log.info("チャットを取得中…")
            chat = fetch_vod_chat(video_id, progress=lambda t: print(f"\r  チャット取得: {int(t)//60} 分まで", end="", file=sys.stderr))
            print(file=sys.stderr)
            save_chat_jsonl(chat, cache)
    log.info("チャット %d 件", len(chat))

    src = VodSource(url, work, quality=args.quality, full_download=args.full_download)
    result = process(cfg, src, chat, Path(cfg.output_dir) / f"{channel or 'vod'}_{video_id}",
                     duration=duration, clips=clips, channel=channel, stream_title=title, dry_run=args.dry_run,
                     clip_vod=(video_id, 0.0),
                     # API で持ち主を確認できないときは指定されたチャンネルを使う
                     # (違っていれば Twitch 側が拒否し、1 回で作成をやめる)
                     clip_owner=owner or channel)
    _print_result(result.highlights)
    if not args.dry_run and result.highlights and not any(h.output_path for h in result.highlights):
        log.error("ショートを 1 本も書き出せませんでした (ディスクの空きやネットワークを確認してください)")
        return 1
    return 0


def cmd_latest(cfg: Config, args: argparse.Namespace) -> int:
    from .twitch_api import HelixClient

    if not (cfg.twitch.client_id and cfg.twitch.client_secret):
        print("latest コマンドには TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET が必要です", file=sys.stderr)
        return 2
    helix = HelixClient(cfg.twitch.client_id, cfg.twitch.client_secret)
    videos = helix.get_recent_archives(helix.get_user_id(args.channel), args.count)
    live = helix.get_stream(args.channel)
    for v in reversed(videos):
        if live and _is_current_stream(v, live):
            # 配信中の VOD は途中までしか無い。処理済みにすると残りが作られないので、配信が終わってから作る
            log.info("配信中のため、配信が終わってから作ります: %s %s", v.id, v.title)
            continue
        # auto / watch で作った VOD もここに記録される
        if v.id in processed_vods(cfg.work_dir) and not args.force:
            log.info("処理済みのためスキップ: %s %s", v.id, v.title)
            continue
        log.info("処理開始: %s %s", v.id, v.title)
        args.vod, args.chat = v.id, None
        args.channel = args.channel.lower()
        if cmd_vod(cfg, args) != 0:
            log.warning("作れなかったため、次回もう一度試します: %s", v.id)
            continue
        if not args.dry_run:
            add_processed_vod(cfg.work_dir, v.id)
    return 0


def _is_current_stream(video, stream: dict) -> bool:
    from .twitch_api import parse_rfc3339

    try:
        return abs(video.created_at.timestamp() - parse_rfc3339(stream["started_at"]).timestamp()) <= 600
    except (KeyError, TypeError, ValueError):
        return True  # 分からなければ、安全側 (配信中とみなして後回し) に倒す


def cmd_login(cfg: Config, args: argparse.Namespace) -> int:
    from .twitch_auth import TwitchAuthError, device_login

    try:
        token = device_login(cfg.twitch.client_id, cfg.twitch.client_secret, cfg.work_dir)
    except TwitchAuthError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{token['login']} でログインしました。auto / watch / vod でクリップも自動で作れます。")
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from .doctor import print_checks, run_checks

    print("動作に必要なものを確認しています…")
    ok = print_checks(run_checks(cfg, (args.channel or "").lower(), online=bool(args.channel)))
    print("準備OKです。" if ok else "NG の項目を準備してください (README / docs/GUIDE.md 参照)。")
    return 0 if ok else 1


def cmd_auto(cfg: Config, args: argparse.Namespace) -> int:
    """配信の検知 → 録画 → ショート作成 (+ Twitch クリップ作成) → 次の配信を待つ、を繰り返す。"""
    from .doctor import print_checks, run_checks
    from .twitch_auth import can_create_clips, load_token
    from .watcher import watch

    channel = args.channel.lower()
    print(f"twitch-shorts 自動モード: {channel}")
    if not print_checks(run_checks(cfg, channel)):
        print("必須の項目 (NG) を準備してから、もう一度起動してください。", file=sys.stderr)
        return 1
    token = load_token(cfg.work_dir)
    has_keys = bool(cfg.twitch.client_id and cfg.twitch.client_secret)
    if args.no_twitch_clips:
        cfg.clips.enabled = False
    elif has_keys and can_create_clips(token, channel):
        cfg.clips.enabled = True
    else:
        if cfg.clips.enabled or token:
            print("※ Twitch クリップは作りません (API キーと、このチャンネルの配信者アカウントでの "
                  "twitch-shorts login が必要です)")
        cfg.clips.enabled = False
    if args.rolling is not None:
        cfg.watch.rolling_minutes = args.rolling
    print("配信が始まると自動で録画し、終わったらショート動画"
          + ("と Twitch クリップ" if cfg.clips.enabled else "") + "を作ります。止めるときは Ctrl+C。")
    watch(cfg, channel, dry_run=args.dry_run)
    return 0


def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    from .watcher import watch

    if args.rolling is not None:
        cfg.watch.rolling_minutes = args.rolling
    watch(cfg, args.channel.lower(), once=args.once, dry_run=args.dry_run)
    return 0


def cmd_local(cfg: Config, args: argparse.Namespace) -> int:
    from .chat import load_chat
    from .pipeline import LocalSource, process

    chat = load_chat(args.chat) if args.chat else []
    clips = []
    if args.clips:
        from .models import ClipRef

        clips = [ClipRef(**c) for c in json.loads(Path(args.clips).read_text(encoding="utf-8"))]
    viewers = None
    if args.viewers:
        from .viewers import load_viewers

        viewers = load_viewers(args.viewers)
    out = Path(cfg.output_dir) / (args.name or Path(args.video).stem)
    result = process(cfg, LocalSource(args.video), chat, out, clips=clips, channel=args.channel or "",
                     stream_title=args.title or "", dry_run=args.dry_run, viewers=viewers)
    _print_result(result.highlights)
    return 0


def cmd_analyze(cfg: Config, args: argparse.Namespace) -> int:
    from datetime import datetime

    from .analytics import analyze_channel, hints, write_outputs
    from .twitch_api import HelixClient

    if not (cfg.twitch.client_id and cfg.twitch.client_secret):
        print("analyze コマンドには TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET が必要です", file=sys.stderr)
        return 2
    helix = HelixClient(cfg.twitch.client_id, cfg.twitch.client_secret)
    channel = args.channel.lower()
    result = analyze_channel(cfg, helix, channel, days=args.days, max_vods=args.vods)
    out = Path(args.output or Path(cfg.output_dir) / f"analysis_{channel}_{datetime.now():%Y%m%d}")
    paths = write_outputs(result, out)
    print(f"クリップ {len(result.clips)} 本を分析しました")
    for h in hints(result):
        print(f"  ・{h}")
    if result.backtest:
        b = result.backtest
        print(f"  人気クリップの検出数: 現在の設定 {b['before']}/{b['popular_clips']} → 推奨設定 {b['after']}/{b['popular_clips']}")
    print(f"レポート: {paths['report']}")
    print(f"推奨設定: {paths['config']}  (使い方: twitch-shorts -c {paths['config']} ...)")
    return 0


def cmd_schedule(cfg: Config, args: argparse.Namespace) -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .schedule import load_schedule

    if args.output:
        cfg.output_dir = args.output
    now = datetime.now(ZoneInfo(cfg.publish.timezone))
    entries = [e for e in load_schedule(cfg) if args.all or datetime.fromisoformat(e["publish_at"]) >= now]
    if not entries:
        print("投稿予定はありません")
        return 0
    for e in entries:
        when = e["publish_at"][:16].replace("T", " ")
        hook = f" 〔{e['hook']}〕" if e.get("hook") else ""
        print(f"{when}  {e['title']}{hook}\n                  {e['path']}")
    return 0


def cmd_feedback(cfg: Config, args: argparse.Namespace) -> int:
    from datetime import datetime

    from .youtube_feedback import analyze_feedback, load_youtube_csv, match_shorts, our_shorts, write_outputs

    results, missing = load_youtube_csv(args.csv)
    matched = match_shorts(results, our_shorts(cfg))
    fb = analyze_feedback(cfg, results, missing)
    out = Path(args.report_dir or Path(cfg.output_dir) / f"feedback_{datetime.now():%Y%m%d}")
    paths = write_outputs(fb, out)
    b = fb.benchmarks
    print(f"ショート {len(results)} 本を読み込みました (作ったショートと一致: {matched} 本)")
    if b["median_stayed_pct"] is not None:
        print(f"  視聴を継続 (中央値): {b['median_stayed_pct']}%  / スワイプ率: {b['median_swipe_pct']}%")
    if b["median_avg_viewed_pct"] is not None:
        print(f"  平均視聴率 (中央値): {b['median_avg_viewed_pct']}%")
    for n in fb.notes:
        print(f"  ※ {n}")
    print(f"レポート: {paths['report']}")
    if fb.recommended:
        print(f"推奨設定: {paths['config']}  (使い方: twitch-shorts -c config.toml -c {paths['config']} ...)")
    return 0


def cmd_chat(cfg: Config, args: argparse.Namespace) -> int:
    from .chat import fetch_vod_chat, save_chat_jsonl
    from .twitch_api import parse_video_id

    video_id = parse_video_id(args.vod)
    chat = fetch_vod_chat(video_id)
    out = args.out or f"chat_{video_id}.jsonl"
    save_chat_jsonl(chat, out)
    print(f"{len(chat)} 件を {out} に保存しました")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twitch-shorts", description="Twitch 配信の盛り上がりを自動でショート動画にする")
    p.add_argument("-c", "--config", action="append",
                   help="設定ファイル (TOML)。複数指定すると後のものが優先。省略時は ./config.toml があれば使う")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def add_render_opts(sp, twitch_clips: bool = True):
        sp.add_argument("-o", "--output", help="出力ディレクトリ")
        sp.add_argument("-n", "--top", type=int, help="作成するショート動画の本数")
        sp.add_argument("--layout", choices=["blur", "crop", "facecam"])
        sp.add_argument("--llm", action="store_true", help="Claude で候補を評価しタイトルを付ける")
        sp.add_argument("--transcribe", action="store_true", help="faster-whisper で字幕を付ける")
        sp.add_argument("--no-subtitles", action="store_true")
        sp.add_argument("--dry-run", action="store_true", help="検出だけ行い動画は作らない")
        if twitch_clips:
            sp.add_argument("--twitch-clips", action="store_true",
                            help="検出した場面を Twitch の公式クリップとしても作る (要 login)")

    sp = sub.add_parser("vod", help="VOD からショート動画を作る")
    sp.add_argument("vod", help="VOD の URL または ID")
    sp.add_argument("--chat", help="チャットファイル (取得済みの場合)")
    sp.add_argument("--channel", help="チャンネル名 (タイトル用)")
    sp.add_argument("--no-clips", action="store_true", help="既存クリップをシグナルに使わない")
    sp.add_argument("--quality", default="best", help="yt-dlp の format 指定 (例: 720p60/best)")
    sp.add_argument("--full-download", action="store_true", help="区間ごとでなく VOD 全体をダウンロード")
    add_render_opts(sp)
    sp.set_defaults(func=cmd_vod)

    sp = sub.add_parser("latest", help="チャンネルの最新アーカイブを処理 (処理済みはスキップ)")
    sp.add_argument("channel")
    sp.add_argument("--count", type=int, default=1, help="新しい順に何本までを対象にするか")
    sp.add_argument("--force", action="store_true", help="処理済みでも再処理する")
    sp.add_argument("--no-clips", action="store_true")
    sp.add_argument("--quality", default="best")
    sp.add_argument("--full-download", action="store_true")
    add_render_opts(sp)
    sp.set_defaults(func=cmd_latest)

    sp = sub.add_parser("auto", help="【おすすめ】起動しておくだけで配信ごとに自動でショートとクリップを作る")
    sp.add_argument("channel")
    sp.add_argument("--rolling", type=int, help="配信中も N 分ごとに作る (同じ PC で配信している場合は 0 推奨)")
    sp.add_argument("--no-twitch-clips", action="store_true", help="Twitch の公式クリップは作らない")
    add_render_opts(sp, twitch_clips=False)  # auto はログイン状態から自動で決める
    sp.set_defaults(func=cmd_auto)

    sp = sub.add_parser("login", help="配信者アカウントで Twitch にログイン (公式クリップの自動作成用)")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("doctor", help="必要なものが揃っているか確認する")
    sp.add_argument("channel", nargs="?", help="指定すると Twitch API への接続も確認")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("watch", help="配信を監視して自動で録画・切り抜き")
    sp.add_argument("channel")
    sp.add_argument("--once", action="store_true", help="1 回の配信を処理したら終了")
    sp.add_argument("--rolling", type=int, help="配信中も N 分ごとにショートを作る (0 で配信後のみ)")
    add_render_opts(sp)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("local", help="手元の動画ファイルから作る")
    sp.add_argument("video")
    sp.add_argument("--chat", help="チャットファイル (JSONL / TwitchDownloader JSON など)")
    sp.add_argument("--clips", help="既存クリップ情報の JSON ([{offset, duration, views}])")
    sp.add_argument("--viewers", help="同時視聴者数の記録 (watch モードの viewers.jsonl や offset,viewers の CSV)")
    sp.add_argument("--channel")
    sp.add_argument("--title", help="配信タイトル (LLM への文脈)")
    sp.add_argument("--name", help="出力サブディレクトリ名")
    add_render_opts(sp, twitch_clips=False)  # 手元のファイルには VOD が無いのでクリップは作れない
    sp.set_defaults(func=cmd_local)

    sp = sub.add_parser("analyze", help="人気クリップを分析し、レポートと推奨設定を作る")
    sp.add_argument("channel")
    sp.add_argument("--days", type=int, default=60, help="何日前までのクリップを対象にするか")
    sp.add_argument("--vods", type=int, default=3, help="チャットまで分析する VOD の数 (多いほど時間がかかる)")
    sp.add_argument("-o", "--output", help="出力ディレクトリ")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("schedule", help="投稿予定を表示する")
    sp.add_argument("--all", action="store_true", help="過ぎた予定も表示")
    sp.add_argument("-o", "--output", help="出力ディレクトリ")
    sp.set_defaults(func=cmd_schedule)

    sp = sub.add_parser("feedback", help="YouTube Studio の数値を取り込んで検出設定を調整する")
    sp.add_argument("csv", help="YouTube Studio 詳細モードのエクスポート (Table data.csv) か手入力の CSV")
    # -o/--output は他コマンドでは output_dir の上書きなので、ここではレポート先として別名にする
    sp.add_argument("-o", "--report-dir", dest="report_dir", help="レポートの出力先")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("chat", help="VOD のチャットを保存する")
    sp.add_argument("vod")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_chat)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    config_path = args.config or (["config.toml"] if Path("config.toml").exists() else None)
    cfg = load_config(config_path)
    _common_overrides(cfg, args)
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
