import json
import shutil
import subprocess

from twitch_shorts import cli, watcher
from twitch_shorts.audio import probe_video_size
from twitch_shorts.config import Config


def test_record_and_process_with_mocked_stream(make_stream, tmp_path, monkeypatch):
    video, chat_path = make_stream(duration=100, events=(50,))

    def fake_recording(channel, out_path, recorder, quality):
        # 「配信」を MPEG-TS として書き出して終了するプロセス
        return subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c", "copy",
                                 "-f", "mpegts", str(out_path)], stderr=subprocess.PIPE)

    class FakeChatRecorder:
        def __init__(self, channel, out_path, start_time, token, nick):
            self.out_path, self.count = out_path, 0

        def start(self):
            shutil.copy(chat_path, self.out_path)

        def stop(self):
            pass

    monkeypatch.setattr(watcher, "start_live_recording", fake_recording)
    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChatRecorder)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    class FakeHelix:
        def get_stream(self, login):
            return {"viewer_count": 12}

    done = watcher.record_and_process(cfg, "yuuki_ftw", helix=FakeHelix())
    [viewers_log] = list((tmp_path / "work" / "yuuki_ftw").glob("*/viewers.jsonl"))
    assert '"viewers": 12' in viewers_log.read_text()
    assert len(done) == 1
    assert done[0].start <= 54 <= done[0].end
    assert probe_video_size(done[0].output_path) == (360, 640)


def test_wait_until_live_polls(monkeypatch):
    states = iter([False, False, True])

    class Checker:
        def is_live(self, channel):
            return next(states)

    sleeps = []
    monkeypatch.setattr(watcher.time, "sleep", sleeps.append)
    cfg = Config()
    watcher.wait_until_live(cfg, "yuuki_ftw", Checker())
    assert sleeps == [cfg.watch.poll_interval] * 2


def test_cli_local_dry_run(make_stream, tmp_path, capsys):
    video, chat_path = make_stream(duration=100, events=(50,))
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n')
    rc = cli.main(["-c", str(conf), "local", str(video), "--chat", str(chat_path), "--channel", "yuuki_ftw",
                   "-o", str(tmp_path / "out"), "--dry-run", "-n", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "yuuki_ftw 切り抜き #1" in out and "(dry-run)" in out
    report = json.loads((tmp_path / "out" / "stream" / "highlights.json").read_text(encoding="utf-8"))
    assert len(report["highlights"]) == 1


def test_cli_help_lists_commands(capsys):
    try:
        cli.main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    for cmd in ("vod", "latest", "watch", "local", "chat"):
        assert cmd in out
