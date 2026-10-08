# twitch-shorts

Twitch の配信から**盛り上がった場面を自動で見つけて**、YouTube Shorts / TikTok 向けの
**縦型ショート動画 (1080×1920, 15〜59 秒)** と **Twitch の公式クリップ** を作るツールです。

> **はじめての方は [使い方ガイド (docs/GUIDE.md)](docs/GUIDE.md) をご覧ください。**
> インストールから、PC で起動しておくだけの全自動化までを順番に説明しています。

```bash
twitch-shorts doctor yuuki_ftw   # 準備ができているか確認
twitch-shorts login              # (任意) Twitch クリップも自動で作る場合
twitch-shorts auto yuuki_ftw     # 起動しておくだけで、配信ごとに自動でショートとクリップを作る
```

```
配信/VOD ─┬─ チャット (流速・「草」「KEKW」「クリップ」等のワード)
          ├─ 音声 (叫び・笑い声などの音量ピーク)
          ├─ 視聴者が作った既存クリップ (Twitch API)
          └─ 同時視聴者数の増加 (watch / auto モードで記録)
                 │
                 ▼  各シグナルを「平常時からのずれ」に正規化して合成 → ピーク検出
          ハイライト候補
                 │  (任意) faster-whisper で文字起こし → 文の切れ目に区間を合わせる
                 │  (任意) Claude で評価・タイトル・引きの言葉・カテゴリ・ハッシュタグ
                 ▼
          ffmpeg で 9:16 に変換 + 冒頭の先見せ + テロップ焼き込み + 音量正規化 (-14 LUFS)
                 ▼
          output/<チャンネル>/<日時>/short_01_HHMMSS.mp4 … + index.html (確認ページ)
          + Twitch 公式クリップ (ログイン時) + 投稿予定表 (毎日 19:00 に 1 本)
```

## できること

| コマンド | 用途 |
| --- | --- |
| `twitch-shorts auto yuuki_ftw` | **【おすすめ】全自動**。配信を検知して録画し、配信後にショートと Twitch クリップを作って次の配信を待つ |
| `twitch-shorts login` | 配信者アカウントで Twitch にログイン (公式クリップの自動作成用) |
| `twitch-shorts doctor [チャンネル]` | 必要なものが揃っているか確認 |
| `twitch-shorts watch yuuki_ftw` | **配信を監視**。始まったら自動で録画＋チャット記録し、終わったらショートを作る (常駐) |
| `twitch-shorts latest yuuki_ftw` | **最新アーカイブを処理**。処理済み VOD はスキップするので cron / GitHub Actions 向け |
| `twitch-shorts vod <URL or ID>` | 指定した VOD を処理 |
| `twitch-shorts local 録画.mp4 --chat chat.json` | 手元の録画ファイル＋チャットログから作る |
| `twitch-shorts chat <URL or ID>` | VOD のチャットを JSONL で保存するだけ |
| `twitch-shorts analyze yuuki_ftw` | **よく見られたクリップを分析**し、レポートと推奨設定 (`config.tuned.toml`) を作る |
| `twitch-shorts schedule` | 作ったショートの**投稿予定** (毎日同じ時刻に 1 本) を表示 |
| `twitch-shorts feedback "Table data.csv"` | **YouTube の結果** (インプレッション・スワイプ率・維持率) を取り込み、検出設定を調整 |

共通オプション: `-n 本数` / `--layout blur|crop|facecam` / `--transcribe` (字幕) / `--llm` (AI 評価・タイトル) / `--dry-run` (検出のみ)

## セットアップ

必要なもの: Python 3.10+、ffmpeg、日本語フォント (例: Noto Sans CJK JP)

```bash
# Ubuntu の例
sudo apt install ffmpeg fonts-noto-cjk
# macOS の例
brew install ffmpeg && brew install --cask font-noto-sans-cjk-jp

pip install -e .                 # 基本機能
pip install -e '.[transcribe]'   # 字幕を付ける場合 (faster-whisper)
pip install -e '.[llm]'          # Claude で評価・タイトル生成する場合
cp config.example.toml config.toml
```

### Twitch API キー (推奨・任意)

[Twitch Developer Console](https://dev.twitch.tv/console) でアプリを登録し、Client ID と Client Secret を
`config.toml` か環境変数に設定します。

```bash
export TWITCH_CLIENT_ID=xxxx
export TWITCH_CLIENT_SECRET=yyyy
```

無くても `vod` / `watch` / `local` は動きますが、次が使えなくなります:
既存クリップのシグナル、`latest` コマンド、API による配信開始検知 (代わりに yt-dlp で確認します)。

### Claude (任意)

`--llm` を付けると、検出した候補 (チャット抜粋・文字起こし) を Claude に渡して
「単体で見て面白いか」を 0〜10 で評価し直し、タイトルを付けます。`ANTHROPIC_API_KEY` が必要です。
失敗・拒否された場合はシグナルだけの順位で続行します。

## 使い方の例 (yuuki_ftw)

```bash
# 1) 配信中ずっと動かしておく (配信が終わると自動で作成、次の配信を待つ)
twitch-shorts watch yuuki_ftw

#    配信中も 30 分ごとにショートを作りたい場合
twitch-shorts watch yuuki_ftw --rolling 30

# 2) 配信後にまとめて作る (cron で毎朝実行など)
twitch-shorts latest yuuki_ftw --transcribe --llm

# 3) 特定の VOD
twitch-shorts vod https://www.twitch.tv/videos/1234567890 -n 3 --layout facecam
```

### GitHub Actions で全自動 (サーバー不要)

`.github/workflows/auto-shorts.yml` が毎日 06:07 (JST) に人気クリップを分析して検出設定を調整し、
その設定で最新アーカイブを処理して、動画と分析レポートを Actions の Artifacts にアップロードします。リポジトリの
Settings → Secrets and variables → Actions で以下を設定すると有効になります。

- Secrets: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (任意で `ANTHROPIC_API_KEY`)
- Variables: `TWITCH_CHANNEL` (省略時は `yuuki_ftw`)

手動実行 (Run workflow) で VOD の URL を指定することもできます。

## ショートの型

タルレミ・エラさんの note「[OW動画投稿講座](https://note.com/tallemi_ella/n/na6e518039897)」の
考え方を参考に、作るショートを次の型にそろえています。

| 講座のポイント | このツールでの実装 | 設定 (`[render]` / `[publish]`) |
| --- | --- | --- |
| 初手の 3 秒が命。引きのある言葉でスワイプさせない | 盛り上がりの瞬間を**冒頭に 2 秒先見せ**してから本編を流す。`--llm` 時は「引きの言葉」(10 文字以内) を最初の 3 秒に大きく表示 | `hook_seconds`, `hook_text_seconds` |
| 配置は決めたら絶対に変えない (見慣れた配置が最強のスワイプ防止) | タイトル・テロップの位置とレイアウトは設定で固定し、全動画で同じにする | `layout`, `caption_position` |
| テロップは真ん中から少し上に固定、10 文字以上なら途中でカット | 字幕・フック文を画面の上から 38% の位置に固定。10 文字を超える発話は句読点で区切って次のテロップに分けて順番に表示 | `caption_position`, `caption_max_chars` |
| 毎日同じ時間に投稿、解説系以外は 1 日 1 本まで | 作ったショートを良い順に**毎日 19:00 の枠へ 1 本ずつ**割り当てた投稿予定表 (`output/schedule.csv`) を作る | `post_time`, `posts_per_day` |
| 見るべき指標はインプレッション・スワイプ率・維持率 | `feedback` コマンドで YouTube Studio の数値を取り込み、スワイプされにくかったショートの傾向に検出設定を寄せる (下記) | — |

```bash
twitch-shorts schedule        # これからの投稿予定を表示
```

※ 字幕は `--transcribe`、引きの言葉は `--llm` を付けたときに入ります (付けない場合も先見せは入ります)。

## YouTube の結果で改善する (feedback)

投稿したショートの **インプレッション・スワイプ率 (視聴を継続)・維持率 (平均視聴率)** を取り込み、
次の切り抜きに反映します。

1. YouTube Studio → アナリティクス → **詳細モード** を開き、対象期間を選ぶ
2. 列に「インプレッション数」「平均視聴率」「視聴を継続」(英語 UI では Impressions / Average percentage viewed /
   Stayed to watch・Viewed vs. swiped away) を追加して、**現在のビューをエクスポート** → `Table data.csv`
3. 取り込む

```bash
twitch-shorts feedback "Table data.csv"
# → output/feedback_YYYYMMDD/report.md, feedback.json, config.feedback.toml
twitch-shorts -c config.toml -c output/feedback_YYYYMMDD/config.feedback.toml latest yuuki_ftw
```

- 列名は英語/日本語の表記ゆれを吸収します。エクスポートに「視聴を継続」が入らない場合は、ショートの画面の数値を
  書き写した CSV (`title,impressions,stayed_pct,avg_viewed_pct`) でも動きます。
- このツールで作ったショートとは**タイトル**で突き合わせます (投稿時に付けた `#shorts` などのハッシュタグは無視)。
- レポートには、チャンネルの中央値を基準にした各ショートの診断 (冒頭でスワイプされがち / 途中で離脱されがち /
  表示回数が少ない)、スワイプされにくかったショートで強かったシグナル、長さと維持率の関係、引きの言葉の有無による差が出ます。
- 突き合わせできたショートが 4 本以上あれば、シグナルの重み (1 点差ごとに 10%、±30% まで) と最大の長さを調整した
  推奨設定を出します。外部の目安 (維持率 60% など) ではなく、自分のチャンネルの中央値と比べます。
- GitHub Actions では、リポジトリに `feedback/youtube.csv` を置いておくと毎日の処理で自動的に反映されます。

## 分析して「みんなが見てくれる」ショートに寄せる

Twitch のダッシュボードの分析データは API で取得できないため、公式 API で取れる
**視聴者が作ったクリップとその再生数**を「みんなが見たがった場面」の実績として分析します。

```bash
twitch-shorts analyze yuuki_ftw --days 60 --vods 3
# → output/analysis_yuuki_ftw_YYYYMMDD/report.md, analysis.json, config.tuned.toml

# 推奨設定を自分の設定に重ねて使う (後に書いた方が優先)
twitch-shorts -c config.toml -c output/analysis_yuuki_ftw_YYYYMMDD/config.tuned.toml latest yuuki_ftw
```

レポートでわかること:

- よく見られているクリップの一覧、長さ・配信内の位置 (序盤/中盤/終盤)・ゲーム・曜日/時間帯ごとの再生数
- 人気クリップの場面で強く出ていたシグナル (チャット流速 / 盛り上がりワードなど) → 重みを調整
- 人気の場面のチャットに特徴的な言葉・エモート (チャンネル独自のエモートなど) → 盛り上がりワードに追加
- 盛り上がりのピークがクリップのどこにあるか → 前フリ (`pre_roll`) / 余韻 (`post_roll`) の長さを調整
- 答え合わせ: 現在の設定と推奨設定で、人気クリップの場面をいくつ検出できるか
- 次の動画づくりのヒント (はっきり差が出た項目だけを文章で提示)

「人気」は期間内の再生数上位 25% (最低 3 本) のクリップです。VOD が削除済みのクリップはチャット分析から除外します。

### 同時視聴者数

`watch` モードでは、Twitch API の認証情報があれば配信中の同時視聴者数を 1 分ごとに記録し
(`work/<チャンネル>/<日時>/viewers.jsonl`)、**視聴者が増えた場面**を検出のシグナルに加えます。
数人程度の出入りは無視し、API の反映遅れ (`viewer_delay`) も補正します。
記録したファイルは `local --viewers viewers.jsonl` でも使えます。

## レイアウト

- `blur` (既定): 元映像を中央に、背景に拡大ぼかし映像。どんな配信でも破綻しない
- `crop`: 中央を 9:16 で切り抜き。画面中央に見どころがあるゲーム向け
- `facecam`: 上に顔カメラ、下にゲーム画面。`config.toml` の `render.facecam` で
  顔カメラの位置を元映像に対する比率 `[x, y, 幅, 高さ]` で指定 (例: 右下 1/4 なら `[0.75, 0.75, 0.25, 0.25]`)

## 検出の調整

`config.toml` の `[detect]` で調整できます。

- 何も検出されない → `min_score` を下げる (小規模チャンネルは 3.0 前後)
- 関係ない場面が混ざる → `min_score` を上げる / `weight_audio` を下げる
- 切り出しが早すぎる・遅すぎる → `chat_delay`, `pre_roll`, `post_roll`
- チャンネル独自の盛り上がりコメントやエモート → `[detect.keywords]` に追加

`output/.../highlights.json` に各ハイライトのスコア内訳 (chat / keywords / audio / clips) と
チャット抜粋が出るので、調整の参考にしてください。

## 確認ページと Twitch クリップ

- 実行ごとに `index.html` (確認ページ) を作ります。盛り上がりスコアのグラフ、カテゴリ (面白い / スーパープレイ /
  ほっこり / ネタ・名場面) とスコアでの絞り込み、各ショートのプレビュー、タイトル・説明文・ハッシュタグのコピー、
  Twitch クリップと VOD の該当箇所へのリンクがあります ([KumoMoments](https://kumotools.com/kumomoments) の
  タイムライン表示とカテゴリ分けを参考にしています)。
- `twitch-shorts login` しておくと、検出した場面を Twitch の **Create Clip From VOD** API (オープンベータ) で
  公式クリップとしても作ります (`auto` では自動で有効。`vod` / `latest` は `--twitch-clips`)。
  配信中の録画とその配信の VOD は、録画開始の遅れを補正して位置を合わせます。

## 仕組みと注意点

- **チャット (VOD)**: Twitch 公式 API には VOD のチャットを取得する API が無いため、Twitch の Web
  プレイヤーが使っている GQL (`VideoCommentsByOffsetOrCursor`) を利用しています。**非公式の方法なので
  Twitch 側の変更で動かなくなる可能性があります。** その場合は TwitchDownloader 等で保存したチャット
  JSON を `--chat` で渡せば同じように処理できます (TwitchDownloader / chat-downloader / JSONL 形式に対応)。
- **チャット (ライブ)**: Twitch IRC (`irc.chat.twitch.tv`) に読み取り専用で接続して記録します。
- **既存クリップ**: Helix API `Get Clips` の `vod_offset` (クリップが VOD の何秒目か) を使います。
- **VOD のダウンロード**: 解析には音声のみ (Audio_Only) を取得し、動画はハイライト区間だけを
  yt-dlp でダウンロードするので、長時間配信でも通信量が少なく済みます (`--full-download` で全体取得)。
- **権利について**: 自分のチャンネル、または配信者が切り抜きを許可しているチャンネルでのみ使ってください。
  配信中の BGM など第三者の権利物が含まれる場合、投稿先で制限を受けることがあります。

## 開発

```bash
pip install -e '.[dev]'
pytest -q
```

テストは合成した動画・チャットで検出〜レンダリングまでを実際に実行します。
Twitch への通信部分はモックでテストしています。

## 参考にした情報源

- Twitch Helix API リファレンス (Get Clips / Get Videos / Get Streams / Get Users):
  <https://dev.twitch.tv/docs/api/reference/>
- `Get Clips` のフィールド (`vod_offset` 等) の確認: Twurple の HelixClip ドキュメント
  <https://twurple.js.org/reference/api/classes/HelixClip.html>
- Twitch API でダッシュボードの分析データが取得できないこと: Twitch Developer Forums
  「Access Channel Analytics via API」 <https://discuss.dev.twitch.com/t/access-channel-analytics-via-api/16642>
- VOD チャットを GQL で `contentOffsetSeconds` によりページングする方式: twitch-vod-lib
  <https://pypi.org/project/twitch-vod-lib/>
- Claude API (構造化出力 `output_config.format`、`fallbacks`): Anthropic 公式 SDK ドキュメント
  <https://platform.claude.com/docs>
- yt-dlp (`download_ranges` による区間ダウンロード): <https://github.com/yt-dlp/yt-dlp>
- faster-whisper: <https://github.com/SYSTRAN/faster-whisper>
- ショートの型 (冒頭 3 秒・配置固定・テロップ位置と文字数・投稿頻度・見るべき指標): タルレミ・エラ
  「OW動画投稿講座」 <https://note.com/tallemi_ella/n/na6e518039897>
  (開発環境から本文に直接アクセスできなかったため、検索エンジン経由で確認できた要点に基づいています)
- YouTube ショートの指標 (視聴を継続・平均視聴率) と詳細モードのエクスポート: YouTube ヘルプ
  <https://support.google.com/youtubecreatorstudio/answer/12220281?hl=ja>,
  <https://support.google.com/youtube/answer/9717005?hl=ja>
  (エクスポート CSV の正確な列名は公式に記載が無いため、表記ゆれを吸収する実装にしています)
