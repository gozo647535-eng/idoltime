# idoltime

[現場監督](https://genba-idol.vercel.app/) 用のチケット情報取り込みワーカー。

2026-10-27 に非公開リポジトリ `idollife-ingest-worker` から移した。非公開だと
GitHub Actions の無料枠(月2,000分)に収まらず、10月は24日で止まったため。
公開リポジトリなら標準ランナーの実行時間は無料・無制限になる。

図鑑に登録したアイドルの X アカウントを定期監視し、チケット投稿を Gemini(または
正規表現フォールバック)で解析して、Supabase の `auto_events` テーブルへ書き込みます。
IdolLife 本体は、利用者がお気に入りにしたアイドルの分だけをチケットタブに自動表示します。

元は X→Discord 通知ボット。解析ロジック(twikit / Gemini / Ticketdive スクレイプ)は
そのまま流用し、出力先を Discord から Supabase に変更しています。

> ⚠️ このリポジトリは**公開**されている。機密情報(Supabaseのservice_roleキー・
> Groq/Geminiの鍵・Xのcookie)は**すべて GitHub Secrets** に置き、ファイルにも
> コミットメッセージにも絶対に書かないこと(`.gitignore` で `cookies.json` /
> `accounts.json` を除外済み)。Secrets に入れた値は公開されない。
>
> コミットの作者にも実際のメールアドレスを入れないこと。移すときに、履歴の
> 作者アドレスは GitHub の匿名アドレスに書き換えてある。

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
| `GROQ_API_KEY` | Groq console の API キー。公式サイトの文章を予定に変える一次エンジン |
| `GEMINI_API_KEY` | Google AI Studio の Gemini API キー。Groq が無いときの予備 |

**`GROQ_API_KEY` と `GEMINI_API_KEY` の両方が無いと、公式サイトからは予定が1件も作られない。**
(2026-10-27 の初回実行は両方未設定で、30サイトを巡回して0件だった。ページから日付は
読めているのに、予定に変える工程が動かない。)
| `X_COOKIES_JSON` | 既存ボットの `cookies.json` の**中身をそのまま**貼り付け |
| `ACCOUNTS_JSON` | (任意)手動で追加ピン留めする監視リスト。通常は不要 |

`cookies.json` が X 側で失効した場合のフォールバックとして、任意で
`X_USERNAME` / `X_EMAIL` / `X_PASSWORD` も登録できます。

### 3. 監視対象は図鑑から自動生成
監視するXアカウントは、**IdolLife の図鑑に登録されたアイドルのXリンクから自動的に作られます**。
手動でリストを維持する必要はありません。ワーカーは毎回:

1. `catalog.json`(図鑑の公式アイドル。運営が配信)の各アイドルのXリンク
2. 有志が承認済みで追記したXリンク(`idol_info`)

を集め、`x.com` / `twitter.com` のURLからハンドルを抽出し、対応する `idol_slug` に紐づけます。
つまり **図鑑にアイドルのXアカウントを登録すれば、自動で監視対象に加わります**。

- 図鑑にXリンクを増やす方法: 運営が `catalog.json` の該当アイドルの `links` にXを追記
  (`{"label":"X","url":"https://x.com/ハンドル"}`)してデプロイ、または利用者がアプリの
  「このグループの情報を追記」でXを追記 → 運営承認。
- どうしても図鑑に無いアカウントを個別に追加したい場合のみ、`ACCOUNTS_JSON`(任意)に
  `[{"handle":"...","idol_slug":"artist:..."}]` を入れると手動で加えられます。
- 図鑑のデータ元URLを変えたい場合は `CATALOG_URL` を Secret/環境変数で上書きできます
  (既定: `https://genba-idol.vercel.app/catalog.json`)。

### 4. 実行
- 6時間ごとに自動実行されます(`.github/workflows/ingest.yml` の cron を変更可能)。
- **Actions タブ → Ingest ticket posts → Run workflow** で手動実行もできます。
- 初回は各アカウントの最新投稿位置を記録するだけで、通知(取り込み)は次回以降の新規投稿からになります。

## ファイル

- `worker.py` — メイン(X取得・解析・Supabase書き込み)
- `watchlist.py` — 図鑑(catalog.json)+有志追記から監視対象を自動生成
- `sb.py` — Supabase 入出力(標準ライブラリのみ)
- `accounts.example.json` — (任意)手動追加リストの例
- `worker_schema.sql` — `worker_state` テーブル定義
- `.github/workflows/ingest.yml` — 定期実行ワークフロー

## 注意

- twikit は X の**非公式ライブラリ**です。X の仕様変更・アカウント制限・利用規約の影響を
  受ける可能性があります。自己責任でご利用ください。
- データセンターIP(GitHub Actions)からのログインは X に警戒されやすく、cookie の
  再取得が必要になる場合があります。その際は `X_COOKIES_JSON` を更新してください。
