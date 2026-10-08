"""配信者アカウントでのログイン (Twitch のデバイスコード方式) とユーザートークンの保存・更新。

Twitch の公式クリップを作るには、配信者本人 (または編集者) の許可が付いたユーザートークンが必要。
``twitch-shorts login`` を一度実行すると、表示された URL でコードを入力するだけで許可でき、
トークンは work_dir に保存されて以後は自動で更新される。

参考: Twitch Developers "Getting OAuth Access Tokens" (Device Code Grant Flow) /
"Refreshing Access Tokens"
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

import requests

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# 自分のチャンネルのクリップ作成 (VOD から / 配信中)
SCOPES = "channel:manage:clips clips:edit"
TOKEN_FILE = "twitch_user_token.json"
CLIP_SCOPE = "channel:manage:clips"

log = logging.getLogger(__name__)


class TwitchAuthError(RuntimeError):
    pass


def can_create_clips(token: dict | None, channel: str = "") -> bool:
    """このトークンで (指定チャンネルの) クリップを作れるか。クリップは配信者本人のアカウントで作る。"""
    if not token or CLIP_SCOPE not in (token.get("scopes") or []):
        return False
    return not channel or token.get("login", "").lower() == channel.lower()


def token_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / TOKEN_FILE


def device_login(client_id: str, client_secret: str = "", work_dir: str | Path = "work",
                 show: Callable[[str, str], None] | None = None,
                 session: requests.Session | None = None, sleep=time.sleep) -> dict:
    """デバイスコード方式でログインし、トークンを保存して返す。

    show(url, code): ユーザーに「この URL を開いてコードを入力して」と伝える関数。
    """
    if not client_id:
        raise TwitchAuthError("TWITCH_CLIENT_ID が設定されていません")
    session = session or requests.Session()
    r = session.post(DEVICE_URL, data={"client_id": client_id, "scopes": SCOPES}, timeout=30)
    if r.status_code != 200:
        raise TwitchAuthError(f"ログインを開始できませんでした: {r.status_code} {r.text[:200]}")
    dev = r.json()
    (show or _default_show)(dev["verification_uri"], dev.get("user_code", ""))
    interval = float(dev.get("interval", 5))
    deadline = time.time() + float(dev.get("expires_in", 1800))
    data = {"client_id": client_id, "scopes": SCOPES, "device_code": dev["device_code"], "grant_type": DEVICE_GRANT}
    if client_secret:
        data["client_secret"] = client_secret
    while time.time() < deadline:
        sleep(interval)
        t = session.post(TOKEN_URL, data=data, timeout=30)
        if t.status_code == 200:
            token = _with_identity(t.json(), session)
            save_token(work_dir, token)
            return token
        message = (t.json() if t.headers.get("content-type", "").startswith("application/json") else {}).get("message", "")
        if message == "authorization_pending":
            continue
        if message == "slow_down":
            interval += 5
            continue
        raise TwitchAuthError(f"ログインできませんでした: {t.status_code} {t.text[:200]}")
    raise TwitchAuthError("ログインの有効期限が切れました。もう一度 twitch-shorts login を実行してください")


def _default_show(url: str, code: str) -> None:
    print("ブラウザで次の URL を開き、表示されたコードを確認して「許可」してください:")
    print(f"  {url}")
    if code:
        print(f"  コード: {code}")


def _with_identity(token: dict, session: requests.Session) -> dict:
    v = session.get(VALIDATE_URL, headers={"Authorization": f"OAuth {token['access_token']}"}, timeout=30)
    if v.status_code != 200:
        raise TwitchAuthError(f"トークンを確認できませんでした: {v.status_code} {v.text[:200]}")
    info = v.json()
    return {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", ""),
        "expires_at": time.time() + float(token.get("expires_in", 3600)),
        "user_id": info.get("user_id", ""),
        "login": info.get("login", ""),
        "scopes": info.get("scopes", []),
    }


def save_token(work_dir: str | Path, token: dict) -> None:
    p = token_path(work_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 書き込み途中で電源が落ちても壊れないよう、別ファイルに書いてから置き換える
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)  # 自分だけが読めるように (Windows では無視される)
    except OSError:
        pass
    os.replace(tmp, p)


def load_token(work_dir: str | Path) -> dict | None:
    p = token_path(work_dir)
    if not p.exists():
        return None
    try:
        token = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("ログイン情報 (%s) が壊れています。twitch-shorts login をやり直してください", p)
        return None
    return token if isinstance(token, dict) and token.get("access_token") else None


class UserToken:
    """保存したユーザートークン。期限が近ければ refresh_token で自動更新する。"""

    def __init__(self, client_id: str, client_secret: str, work_dir: str | Path,
                 session: requests.Session | None = None):
        token = load_token(work_dir)
        if not token:
            raise TwitchAuthError("Twitch にログインしていません。先に twitch-shorts login を実行してください")
        self.client_id = client_id
        self.client_secret = client_secret
        self.work_dir = work_dir
        self.session = session or requests.Session()
        self.token = token

    @property
    def user_id(self) -> str:
        return self.token.get("user_id", "")

    @property
    def login(self) -> str:
        return self.token.get("login", "")

    def access_token(self, force_refresh: bool = False) -> str:
        if force_refresh or time.time() > float(self.token.get("expires_at", 0)) - 120:
            self._refresh()
        return self.token["access_token"]

    def _refresh(self) -> None:
        if not self.token.get("refresh_token"):
            raise TwitchAuthError("トークンの期限が切れました。twitch-shorts login をやり直してください")
        data = {"client_id": self.client_id, "grant_type": "refresh_token",
                "refresh_token": self.token["refresh_token"]}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        r = self.session.post(TOKEN_URL, data=data, timeout=30)
        if r.status_code != 200:
            raise TwitchAuthError(f"トークンを更新できませんでした (twitch-shorts login をやり直してください): "
                                  f"{r.status_code} {r.text[:200]}")
        body = r.json()
        self.token.update({
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token", self.token["refresh_token"]),
            "expires_at": time.time() + float(body.get("expires_in", 3600)),
        })
        save_token(self.work_dir, self.token)
