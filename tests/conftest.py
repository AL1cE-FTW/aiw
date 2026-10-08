import json
import random
import subprocess

import pytest


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeSession:
    """requests.Session の代わり。呼び出しを記録し、用意したレスポンスを順に返す。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        return self.handler("GET", url, params, None)

    def post(self, url, data=None, headers=None, timeout=None, params=None):
        self.calls.append(("POST", url, data if params is None else params, headers))
        return self.handler("POST", url, params, data)


@pytest.fixture
def make_stream(tmp_path):
    """テスト用の配信動画 (指定区間だけ音が大きい) と、同じ区間で盛り上がるチャットを作る。"""

    def _make(duration=120, events=(40,), size="320x180"):
        video = tmp_path / "stream.mp4"
        loud = "+".join(f"between(t,{e},{e + 8})" for e in events) or "0"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15:duration={duration}",
             "-f", "lavfi", "-i", f"sine=frequency=300:duration={duration}",
             "-filter_complex", f"[1:a]volume='if({loud},1.0,0.05)':eval=frame[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             str(video)],
            check=True,
        )
        rng = random.Random(0)
        chat = tmp_path / "chat.jsonl"
        with open(chat, "w", encoding="utf-8") as f:
            for t in range(duration):
                for _ in range(rng.choice([0, 1, 1, 2])):
                    f.write(json.dumps({"offset": t + rng.random(), "user": f"u{rng.randint(0, 200)}",
                                        "text": rng.choice(["こんにちは", "わこつ"])}, ensure_ascii=False) + "\n")
            for e in events:
                for t in range(e + 3, e + 13):
                    for _ in range(12):
                        f.write(json.dumps({"offset": t + rng.random(), "user": f"v{rng.randint(0, 9999)}",
                                            "text": rng.choice(["草", "KEKW", "うおおお", "クリップ"])},
                                           ensure_ascii=False) + "\n")
        return video, chat

    return _make
