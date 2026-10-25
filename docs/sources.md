# 収集源ごとの取り方と、APIの有無

「APIで取れば節約できないか」を調べた結果を残す。**結論は「APIがある所は
すでに全部APIを使っている」。** 同じ調査を繰り返さないための記録。

調べ方は、Chromium で実際にページを開いて飛んでいる通信を記録し、
JSON を返すエンドポイントがあるかを見る方法（推測ではない）。

調査日: 2026-10-25

## すでに構造化データから取っているもの

| ソース | 取り方 | 置き場所 |
|---|---|---|
| TimeTree | `GET /api/v2/public_calendars/<alias>/public_events` | `timetree.py` |
| Googleカレンダー | iCalフィード `calendar/ical/<id>/public/basic.ics`（キー不要） | `gcal.py` |
| e+ | ページ内の埋め込みJSON（`startDate` / `location.name`） | `eplus.py` |
| TicketDive | `__NEXT_DATA__` のJSON | `ticketdive.py` |
| Yahooパスマーケット | 埋め込みJSON | `yahoo_rt.py` |

### TimeTree のAPIについて分かっていること

必要なヘッダは `X-TimeTreeA` だけ。これが無いと `{"error":{"code":-401}}`
で 400 が返る。ブラウザは `x-csrf-token`（ページの `<meta name="csrf-token">`）
と `x-timetreea: web/2.1.0/ja` を送っているが、**`web/1.0.0/ja` だけでも
csrf 無しで通る**（2026-10-25 時点で確認）。

`from` / `to`（ミリ秒）は**付けなくてよい**。付けない場合も「今日以降の
すべて」が返る。実測:

```
tenshinranman  指定なし 27件 2026-10-27〜2027-01-18
               from/to を 2027-12-31 まで指定 → 27件 同じ
my_stage       指定なし 34件 2026-10-24〜2027-04-07
```

`location_name` / `location_address` / `location_access` / `location_note` /
`url` / `all_day` / `recurrences` などが揃っている。`paging.next` で続きの
有無が分かる。

もし将来 `X-TimeTreeA: web/1.0.0/ja` が弾かれるようになったら、Webアプリが
送っている `web/2.1.0/ja` + csrf トークンの2段取得（ページを取って
`<meta name="csrf-token">` を読み、同じセッションでAPIを叩く）に切り替える。
いま変えないのは、動いているものを推測で触らないため。

## APIが存在しないことを確認したもの

| ソース | 確認した内容 |
|---|---|
| LivePocket | サーバー側でHTMLを生成。JSONは翻訳サービス(wovn)とサポートウィジェットだけ |
| TiGET | AWS WAF のボット判定。`ticketsite.py` がブラウザを使うのはこれが理由 |
| タワーレコード | 同じくサーバー生成HTMLのみ |
| Ticket Village | 素のHTTPで取れるが JSON-LD も `__NEXT_DATA__` も無い |

公式サイト（`sites.py`）は、175件が **154ドメイン**に散っている。
プラットフォームに乗っているものを全部アダプタ化しても
（asobisystem 8 / bitfan.id 6 / fanpla.jp 5 / helloproject 4）
**13%しかカバーできない**ので、汎用のHTML解析を続けるのが妥当。

## 扱っていない販売サイト（意図的）

`ticketvillage.jp` / `l-tike.com` / `atjam.jp` は、TimeTree のメモから
拾った `ticket_url` として保存されるだけで、ページは取りに行っていない。
これは意図的な判断。

- 対象は、チケットURLを持つ 5,636件のうち **62件（1.1%）**（発売日時が
  入っていない、かつ公演が今日以降のもの）
- URLの形がばらばらで**3つ別々のパーサ**が必要。しかも
  `l-tike.com/mmsc/` や `atjam.jp/ticket` のような汎用ページが多く、
  そこには元々イベント個別の発売日時が無い
  （atjam は75件のうち70件が同じ `https://atjam.jp/ticket`、
  ticketvillage は93件が8種類のURLに集まっている）
- 最大の塊（ticketvillage 13884 / TIF学園 73件）は
  `■先行抽選：9/16 ■一般販売：10/9` で既に発売済み。チケ発一覧には出ない
- そのページの「先着」は**規約の定型文**だった。`release.py` で
  「事前販売」を誤判定したのと同じ構造で、間違えると先着タブに出てしまう

1.1% のために、同じ失敗をしに行く価値は無いと判断した。
もし将来これらのサイト発のイベントが増えたら、**atjam から**着手するとよい
（`__NUXT__` に構造化データがあるので、定型文との区別が要らない）。
