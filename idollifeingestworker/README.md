# idollife-ingest-worker

[IdolLife](https://idollife.vercel.app/) 用のチケット情報取り込みワーカー。

図鑑に登録したアイドルの X アカウントを定期監視し、チケット投稿を Gemini(または
正規表現フォールバック)で解析して、Supabase の `auto_events` テーブルへ書き込みます。
IdolLife 本体は、利用者がお気に入りにしたアイドルの分だけをチケットタブに自動表示します。

元は X→Discord 通知ボット。解析ロジック(twikit / Gemini / Ticketdive スクレイプ)は
そのまま流用し、出力先を Discord から Supabase に変更しています。

> ⚠️ このリポジトリは**必ずプライベート**にしてください。ワーカーコードは公開しても
> 問題ありませんが、機密情報(Xのcookie・Supabaseのservice_roleキー・Gemini鍵)は
> **すべて GitHub Secrets** に置き、リポジトリには絶対にコミットしないでください
> (`.gitignore` で `cookies.json` / `accounts.json` を除外済み)。

## 仕組み

```
GitHub Actions(30分ごと)
  → worker.py: twikit で各アカウントの新規投稿を取得
  → Gemini で「発売日時・先着/抽選・券種・URL」を構造化抽出
  → Supabase auto_events に upsert(既読位置は worker_state に保存)
IdolLife(PWA)
  → auto_events を読み、図鑑お気に入りの出演分を表示 + リマインド
```

## セットアップ

### 1. Supabase 側(一度だけ)
IdolLife 本体と同じ Supabase プロジェクトの SQL Editor で `worker_schema.sql` を実行し、
`worker_state` テーブルを作成します(`auto_events` は本体側で作成済み)。

### 2. GitHub Secrets を登録
リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で以下を登録:

| Secret 名 | 内容 |
|---|---|
| `SUPABASE_URL` | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabaseダッシュボード → Project Settings → API → **service_role** キー(絶対非公開) |
| `GEMINI_API_KEY` | Google AI Studio の Gemini API キー(未設定なら正規表現解析にフォールバック) |
| `X_COOKIES_JSON` | 既存ボットの `cookies.json` の**中身をそのまま**貼り付け |
| `ACCOUNTS_JSON` | 監視対象リスト(下記 `accounts.example.json` 形式の JSON をそのまま貼り付け) |

`cookies.json` が X 側で失効した場合のフォールバックとして、任意で
`X_USERNAME` / `X_EMAIL` / `X_PASSWORD` も登録できます。

### 3. 監視対象リスト(`ACCOUNTS_JSON`)
`accounts.example.json` を参考に、監視する X ハンドルと、対応する図鑑アイドルの `slug` を
並べます。`slug` は図鑑の公式アイドルの識別子で、`artist:<グループ名>` 形式です
(例: `artist:CANDY TUNE`)。IdolLife はこの `idol_slug` でお気に入り判定します。

```json
[
  { "handle": "candytune_staff", "idol_slug": "artist:CANDY TUNE" }
]
```

### 4. 実行
- 30分ごとに自動実行されます(`.github/workflows/ingest.yml` の cron を変更可能)。
- **Actions タブ → Ingest ticket posts → Run workflow** で手動実行もできます。
- 初回は各アカウントの最新投稿位置を記録するだけで、通知(取り込み)は次回以降の新規投稿からになります。

## ファイル

- `worker.py` — メイン(X取得・解析・Supabase書き込み)
- `sb.py` — Supabase 入出力(標準ライブラリのみ)
- `accounts.example.json` — 監視対象リストの例
- `worker_schema.sql` — `worker_state` テーブル定義
- `.github/workflows/ingest.yml` — 定期実行ワークフロー

## 注意

- twikit は X の**非公式ライブラリ**です。X の仕様変更・アカウント制限・利用規約の影響を
  受ける可能性があります。自己責任でご利用ください。
- データセンターIP(GitHub Actions)からのログインは X に警戒されやすく、cookie の
  再取得が必要になる場合があります。その際は `X_COOKIES_JSON` を更新してください。
