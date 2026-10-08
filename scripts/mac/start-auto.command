#!/bin/bash
# twitch-shorts auto mode launcher (macOS)。ダブルクリックで起動できます。
# ログイン時に自動で起動するには: システム設定 → 一般 → ログイン項目 → 「+」でこのファイルを追加
cd "$(dirname "$0")/../.." || exit 1
[ -f .venv/bin/activate ] && . .venv/bin/activate
exec twitch-shorts auto "${1:-yuuki_ftw}"
