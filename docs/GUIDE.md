# twitch-shorts 使い方ガイド

配信を自動でチェックして、盛り上がった場面から **縦型ショート動画** と **Twitch のクリップ** を作るツールです。
このガイドは「自分の PC で動かす」前提で、インストールから全自動化までを順番に説明します。

---

## 目次

1. [できること](#1-できること)
2. [準備 (初回だけ)](#2-準備-初回だけ)
3. [全自動で使う (おすすめ)](#3-全自動で使う-おすすめ)
4. [できたものを確認して投稿する](#4-できたものを確認して投稿する)
5. [手動で使うコマンド](#5-手動で使うコマンド)
6. [もっと良くする (字幕・AI・分析)](#6-もっと良くする-字幕ai分析)
7. [設定の早見表](#7-設定の早見表)
8. [困ったとき](#8-困ったとき)
9. [参考にした情報](#9-参考にした情報)

---

## 1. できること

```
配信開始を自動で検知
   ↓
録画 + チャット + 同時視聴者数を記録
   ↓  (配信が終わったら)
盛り上がった場面を検出  … チャットの勢い・「草」「KEKW」などのワード・声の大きさ・視聴者数の増加
   ↓
縦型ショート動画 (mp4) を作る  … 冒頭 2 秒に見どころを先見せ、テロップ位置は固定、音量を調整
   ↓
Twitch の公式クリップも作る (ログインしている場合)
   ↓
確認ページ (index.html) と投稿予定表 (毎日 19:00 に 1 本) ができる
   ↓
次の配信を待つ
```

一度 `twitch-shorts auto yuuki_ftw` を起動しておけば、あとは配信するだけです。

---

## 2. 準備 (初回だけ)

### 2-1. 必要なソフトを入れる

| ソフト | 用途 | 必須 |
| --- | --- | --- |
| Python 3.10 以上 | ツール本体 | ○ |
| ffmpeg | 動画の解析・書き出し | ○ |
| Git | ダウンロード (ZIP でも可) | △ |
| Streamlink | 録画 (無ければ yt-dlp で録画) | 任意 |

**Windows** (PowerShell で実行):

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install Git.Git
winget install Streamlink.Streamlink   # 任意
```

インストール後、PowerShell を開き直して `python --version` と `ffmpeg -version` が表示されれば OK です。

**Mac** ([Homebrew](https://brew.sh/) を使う場合):

```bash
brew install python ffmpeg git streamlink
```

### 2-2. ツールをダウンロードしてインストール

```bash
git clone https://github.com/AL1cE-FTW/aiw.git
cd aiw
python -m venv .venv
```

仮想環境を有効にします (ターミナルを開くたびに必要):

- Windows: `.venv\Scripts\activate`
- Mac / Linux: `source .venv/bin/activate`

```bash
pip install -e .
copy config.example.toml config.toml   # Mac / Linux は cp config.example.toml config.toml
```

### 2-3. Twitch の API キーを取得する (推奨)

無くても動きますが、**同時視聴者数の記録・人気クリップの分析・Twitch クリップの自動作成** に必要です。

1. [Twitch Developer Console](https://dev.twitch.tv/console) に配信者のアカウントでログイン
2. 「アプリケーションを登録」
   - 名前: 好きな名前 (例: `yuuki-shorts`)
   - OAuth のリダイレクト URL: `http://localhost`
   - カテゴリー: `Application Integration` など
   - クライアントの種類: **機密 (Confidential)**
3. 作成したアプリの「管理」から **クライアント ID** と **クライアントシークレット** をコピー
4. `config.toml` に貼り付け

```toml
[twitch]
client_id = "ここにクライアントID"
client_secret = "ここにクライアントシークレット"
```

> `config.toml` は Git にアップロードされない設定になっています。シークレットは他人に見せないでください。

### 2-4. Twitch にログインする (クリップを自動で作る場合)

```bash
twitch-shorts login
```

表示された URL をブラウザで開き、コードを確認して **配信者のアカウントで「許可」** します。
ログイン情報は `work/twitch_user_token.json` に保存され、以後は自動で更新されます。

### 2-5. 準備できたか確認する

```bash
twitch-shorts doctor yuuki_ftw
```

```
  [OK ] ffmpeg: ...
  [OK ] 録画ツール (streamlink / yt-dlp): streamlink
  [OK ] 日本語フォント: ...
  [OK ] Twitch API キー: 設定済み
  [OK ] Twitch API 接続: yuuki_ftw (ID ...)
  [OK ] Twitch ログイン (クリップ作成): yuuki_ftw でログイン済み
  [-- ] 字幕 (faster-whisper): 任意: ...
```

`NG` が出ている項目だけ準備すれば動きます (`--` は任意の機能です)。

---

## 3. 全自動で使う (おすすめ)

```bash
twitch-shorts auto yuuki_ftw
```

これだけです。起動したままにしておくと:

1. 1 分ごとに配信が始まったか確認
2. 始まったら録画・チャット・視聴者数の記録を開始
3. 配信が終わったら、ショート動画と Twitch クリップを作成
4. また次の配信を待つ

止めるときは `Ctrl + C` を押します (それまでに録画した分は処理されます)。

### 同じ PC で配信している場合

- 動画の書き出しは **配信が終わってから** 行うので、配信中の負荷はほとんどありません (録画とチャット記録だけ)。
- 配信中にもショートを作りたい場合は `--rolling 30` (30 分ごと) を付けますが、配信が重くなることがあるので、
  配信用 PC とは別の PC で動かすときだけにするのがおすすめです。
- 録画は配信と同じ映像を Twitch から受け取って保存します (OBS の録画とは別です)。
  広告が流れた時間も録画に入りますが、配信の時刻と録画の時刻をそろえるためで、ショートの位置合わせに必要です。
- 録画中に PC の再起動などで止まってしまった場合も、次に `auto` を起動したときに残っている録画を処理します。

### PC の起動時に自動で立ち上げる

**Windows**: エクスプローラーで `scripts\windows\register-startup.bat` をダブルクリック。
次回のサインインから自動で `auto` が起動します (最小化されたウィンドウで動きます)。
やめるときは `Win + R` → `shell:startup` で開いたフォルダーの `twitch-shorts-auto.bat` を削除します。
すぐに起動したいときは `scripts\windows\start-auto.bat` をダブルクリックします。

**Mac**: システム設定 → 一般 → ログイン項目 → 「+」で `scripts/mac/start-auto.command` を追加。

**Linux**: `scripts/linux/twitch-shorts-auto.service` のパスを書き換えて
`~/.config/systemd/user/` に置き、`systemctl --user enable --now twitch-shorts-auto` を実行。

> PC がスリープすると録画が止まります。配信する日はスリープしない設定にしておいてください。

---

## 4. できたものを確認して投稿する

### 保存場所

```
output/
  yuuki_ftw/20261008_210000/     ← 配信ごとのフォルダー
    index.html                   ← 確認ページ (ブラウザで開く)
    short_01_004512.mp4          ← ショート動画
    highlights.json              ← 検出結果の詳細
  schedule.csv                   ← 投稿予定表 (Excel で開ける)
```

### 確認ページ (index.html)

ダブルクリックするとブラウザで開きます。

- 配信全体の **盛り上がりスコアのグラフ** と、ショートにした場面
- **カテゴリ** (面白い / スーパープレイ / ほっこり / ネタ・名場面) と **スコア** で絞り込み
- 各ショートのプレビュー、タイトル・説明文・ハッシュタグ、検出の根拠 (チャットの反応など)
- 「タイトル+タグをコピー」ボタン (投稿時にそのまま貼り付けられる)
- 「Twitch クリップ」「VOD で開く」リンク

> カテゴリ・説明文・AI のハッシュタグは `--llm` (6 章) を使ったときに付きます。

### 投稿予定

```bash
twitch-shorts schedule
```

良い順に **毎日 19:00 に 1 本** 割り当てた予定が表示されます (`output/schedule.csv` にも保存)。
YouTube Studio や TikTok の予約投稿にこの順番で登録していくのがおすすめです。
時刻や本数は `config.toml` の `[publish]` で変えられます。

### Twitch クリップ

ログインしていれば、検出した場面が Twitch のクリップとして自動で作られます
(1 配信あたり最大 5 本。`[clips] max_per_stream` で変更)。
確認ページの「Twitch クリップ」から開くと、タイトルの編集・縦型版の作成・共有ができます。

> Twitch のクリップ作成 API (VOD からのクリップ) はオープンベータのため、仕様が変わる可能性があります。
> また、位置が数秒ずれることがあると報告されています。

---

## 5. 手動で使うコマンド

| やりたいこと | コマンド |
| --- | --- |
| 過去の配信 (VOD) から作る | `twitch-shorts vod https://www.twitch.tv/videos/1234567890` |
| 最新のアーカイブを処理 (処理済みは飛ばす) | `twitch-shorts latest yuuki_ftw` |
| 手元の録画ファイルから作る | `twitch-shorts local 録画.mp4 --chat chat.json` |
| どこが検出されるかだけ見る | 上のコマンドに `--dry-run` |
| VOD のチャットだけ保存 | `twitch-shorts chat https://www.twitch.tv/videos/1234567890` |
| Twitch クリップも作る | `vod` / `latest` に `--twitch-clips` (要ログイン。自分のチャンネルのみ) |

よく使うオプション: `-n 3` (本数)、`--layout facecam` (レイアウト)、`--transcribe` (字幕)、`--llm` (AI)。

---

## 6. もっと良くする (字幕・AI・分析)

### 字幕を付ける

```bash
pip install -e ".[transcribe]"
twitch-shorts auto yuuki_ftw --transcribe
```

配信者の声を文字起こしして、画面中央より少し上にテロップとして表示します (10 文字ごとに区切って順番に表示)。
初回は文字起こしモデルのダウンロードがあります。常に使うなら `config.toml` の `[transcribe] enabled = true`。

### AI でタイトル・引きの言葉・ハッシュタグを付ける

```bash
pip install -e ".[llm]"
```

[Anthropic Console](https://console.anthropic.com/) で API キーを作り、環境変数 `ANTHROPIC_API_KEY` に設定します。

```bash
twitch-shorts auto yuuki_ftw --llm
```

Claude が各候補を「単体で見て面白いか」で採点し直し、タイトル・冒頭 3 秒の「引きの言葉」・カテゴリ・
説明文・ハッシュタグを付けます (API の利用料金がかかります)。

### レイアウト

`config.toml` の `[render] layout`:

- `blur` (既定): 元映像を中央に、背景はぼかし
- `crop`: 中央を縦に切り抜き
- `facecam`: 上に顔カメラ、下にゲーム画面。`facecam = [x, y, 幅, 高さ]` で顔カメラの位置を
  元映像に対する割合で指定 (例: 右下 1/4 なら `[0.75, 0.75, 0.25, 0.25]`)

### 人気のクリップから学ぶ (analyze)

```bash
twitch-shorts analyze yuuki_ftw
```

視聴者が作ったクリップの再生数を分析し、どんな場面がよく見られているか (長さ・時間帯・ゲーム・
チャットの言葉など) をレポートにして、検出設定のおすすめ (`config.tuned.toml`) を作ります。

### YouTube の結果から学ぶ (feedback)

YouTube Studio → アナリティクス → 詳細モード で「インプレッション数」「平均視聴率」「視聴を継続」の列を追加して
エクスポートした `Table data.csv` を読み込みます。

```bash
twitch-shorts feedback "Table data.csv"
```

スワイプされにくかったショートの傾向から、検出設定のおすすめ (`config.feedback.toml`) を作ります。

### おすすめ設定を使う

`-c` で設定ファイルを重ねます (後に書いたものが優先)。

```bash
twitch-shorts -c config.toml -c output/analysis_yuuki_ftw_20261008/config.tuned.toml auto yuuki_ftw
```

---

## 7. 設定の早見表

`config.toml` のよく変える項目です (全項目は `config.example.toml` を参照)。

| 項目 | 既定 | 説明 |
| --- | --- | --- |
| `[detect] top_n` | 5 | 1 配信で作る本数 |
| `[detect] min_score` | 4.5 | 小さいほど検出されやすい (小規模チャンネルは 3.0 前後) |
| `[detect] max_duration` | 59 | ショートの最長秒数 |
| `[detect.keywords]` | — | 盛り上がりワードの追加 (例: `"yuukiPog" = 1.5`) |
| `[render] layout` | blur | blur / crop / facecam |
| `[render] hook_seconds` | 2.0 | 冒頭に見どころを先見せする秒数 (0 で無効) |
| `[render] font` | OS に合わせて自動 | 字幕のフォント |
| `[publish] post_time` | 19:00 | 投稿予定の時刻 |
| `[clips] max_per_stream` | 5 | 1 配信で作る Twitch クリップの最大数 |
| `[watch] rolling_minutes` | 0 | 配信中も N 分ごとに作る (0 は配信後のみ) |

---

## 8. 困ったとき

| 症状 | 対処 |
| --- | --- |
| 「ハイライトは見つかりませんでした」 | `[detect] min_score` を 3.0 くらいに下げる。チャットが少ない配信は声の大きさで検出します |
| 字幕・タイトルが □ になる | `[render] font` に PC にある日本語フォント名を指定 (Windows: `Yu Gothic`、Mac: `Hiragino Sans`) |
| 録画が始まらない | `pip install -U yt-dlp` で更新。Streamlink を入れると安定します |
| VOD のチャットが取れない | Twitch 側の変更の可能性があります。TwitchDownloader などで保存したチャットを `--chat` で渡してください |
| Twitch クリップが作られない | `twitch-shorts doctor yuuki_ftw` を実行。未ログインなら `twitch-shorts login` |
| 「VOD が見つかりません」 | Twitch の設定 → 配信 →「過去の配信を保存」をオンにする |
| 「トークンを更新できませんでした」 | `twitch-shorts login` をやり直す |
| 黒い画面がすぐ閉じる (Windows) | `start-auto.bat` から起動するとエラーが表示されたまま止まります |

---

## 9. 参考にした情報

- Twitch API: [Create Clip From VOD / Get Clips / Get Streams / Get Videos](https://dev.twitch.tv/docs/api/reference/)、
  [Clip API の改善と Clip From VOD の公開 (Twitch Developer Forums)](https://discuss.dev.twitch.com/t/introducing-clip-api-improvements-and-clip-from-vod-in-open-beta/64492)、
  [ログイン (Device Code Grant Flow)](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/)
- ショートの作り方: タルレミ・エラ「[OW動画投稿講座](https://note.com/tallemi_ella/n/na6e518039897)」
  (冒頭 3 秒・テロップ位置の固定・毎日同じ時間に投稿・見るべき指標)
- 確認ページ・カテゴリ分け: [KumoMoments](https://kumotools.com/kumomoments) (Kumomo Rin) の
  タイムライン表示と場面のカテゴリ (funny / clutch / wholesome / lore)
- YouTube の指標: [YouTube ヘルプ (ショートの指標)](https://support.google.com/youtubecreatorstudio/answer/12220281?hl=ja)、
  [詳細モードとエクスポート](https://support.google.com/youtube/answer/9717005?hl=ja)
