def get_ticket_page_rows(today: str, prefix: str = None):
    """今後の行の {tweet_id, ticket_url, sale_datetime} を返す(ticket_url がある行だけ)。

    prefix を渡すとその取り込み元に絞る(例: "yrt:")。
    TimeTree のノートや Googleカレンダーから拾ったリンクには発売日時・券種・
    先着かどうかが入らない(カレンダーに書かれていない)ため、販売ページを
    読んで後から入れるのに使う。実測(2026-10-13): 今後の行が指す対応サイトの
    URLは471件で、Yahoo!リアルタイム検索から入ったものは48件だけだった。
    """
    params = {
        "select": "tweet_id,ticket_url,sale_datetime",
        "hidden": "eq.false",
        "ticket_url": "not.is.null",
        "event_datetime": f"gte.{today}",
        "order": "tweet_id",
        "limit": 1000,
    }
    if prefix:
        params["tweet_id"] = f"like.{prefix}*"
    out = []
    for offset in range(0, 20000, 1000):
        _, rows = _request("GET", "auto_events", params={**params, "offset": offset})
        rows = rows or []
        out += rows
        if len(rows) < 1000:
            break
    return out


def get_next_event_dates(today: str):
    """idol_slug → 次の出演予定日(today以降で最も早い日) を返す。

    巡回の優先度決めに使う。出演時刻(「出演19:35-20:00」)は公演の1〜3日前に
    各グループがカレンダーへ書き込むため、公演が近いグループを先に読まないと
    タイムテーブルを埋められないまま当日を過ぎてしまう。
    """
    out = {}
    for offset in range(0, 20000, 1000):
        _, rows = _request(
            "GET",
            "auto_events",
            params={
                "select": "idol_slug,event_datetime",
                "hidden": "eq.false",
                "event_datetime": f"gte.{today}",
                "order": "tweet_id",
                "limit": 1000,
                "offset": offset,
            },
        )
        rows = rows or []
        for r in rows:
            slug, dt = r.get("idol_slug"), (r.get("event_datetime") or "")[:10]
            if not slug or not dt:
                continue
            if slug not in out or dt < out[slug]:
                out[slug] = dt
        if len(rows) < 1000:
            break
    return out


"""Supabase(PostgREST)への入出力。標準ライブラリのみで依存を最小化。

service_role キーで書き込むため、SUPABASE_SERVICE_KEY は絶対に公開しないこと
(GitHub Secrets にのみ置く)。
"""
import json
import os
import re
import urllib.request
import urllib.parse
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _headers(extra=None):
    h = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _request(method, path, params=None, body=None, headers=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="().,*")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(headers))
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read().decode("utf-8")
            return res.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Supabase {method} {path} failed: HTTP {e.code} {text[:300]}")


# auto_events に後から足した列。SQL を実行していない環境では落とす。無ければ書き込みから外す
# (列が無いまま POST すると PostgREST が 400 を返し、その回が丸ごと失敗する)。
# kind: 行の種類。リリイベは "release"
# numbering: 整理番号の出し方。"advance"(事前に販売) / "onsite"(当日配布)
# performer: 出演者名。図鑑に無いアーティストでも画面に出せるようにする
OPTIONAL_COLUMNS = ("stage_time", "kind", "numbering", "performer")
_available_columns = None


def _drop_missing_columns(rows):
    global _available_columns
    if _available_columns is None:
        _available_columns = set()
        for col in OPTIONAL_COLUMNS:
            try:
                _request("GET", "auto_events", params={"select": col, "limit": "1"})
                _available_columns.add(col)
            except Exception:
                pass
        missing = [c for c in OPTIONAL_COLUMNS if c not in _available_columns]
        if missing:
            print("  auto_events に未追加の列があるため書き込みません: "
                  + ", ".join(missing)
                  + "(SQL: alter table public.auto_events add column "
                  + " text, add column ".join(missing) + " text;)")
    for col in OPTIONAL_COLUMNS:
        if col not in _available_columns:
            for r in rows:
                r.pop(col, None)
    return rows


# URLの終わりになり得ない文字。ノート本文や予定の説明文からURLを拾う経路
# (TimeTree / Googleカレンダー)があるため、末尾に HTML の残り("> など)や
# 見えない文字(全角空白・ハングル埋め字 U+3164・点字空白 U+2800)が付いた
# URLが実際に保存されていた(174行)。リンクが開けず、同じ販売ページとして
# まとまらないので、書き込む直前にここで落とす。
_URL_TAIL = re.compile(
    r'[\s"\'<>（）()「」『』【】、。\u3000\u2800-\u28ff\u3130-\u318f\u200b-\u200f]')


# 投稿やノートには「t-dv.com/XXX」のようにスキームの無い書き方も出てくる。
# そのままでは取得できないので、ここで補って https に寄せる。
_TICKET_HOSTS = ("t-dv.com", "ticketdive.com", "livepocket.jp", "tiget.net")


def clean_url(url):
    """URLに混ざった末尾のゴミを落とし、スキームを補う。URLでなければそのまま返す。"""
    if not url or not isinstance(url, str):
        return url
    m = _URL_TAIL.search(url.strip())
    out = (url.strip()[:m.start()] if m else url.strip()).rstrip('",\'')
    if not out:
        return None
    host = out.split("/")[0].lower().removeprefix("www.")
    if host in _TICKET_HOSTS:
        return "https://" + out
    if out.startswith("http://") and any(h in out for h in _TICKET_HOSTS):
        return "https://" + out[len("http://"):]
    return out


def _clean_urls(rows):
    for r in rows:
        for key in ("ticket_url", "tweet_url"):
            if r.get(key):
                r[key] = clean_url(r[key])
    return rows


def get_ticket_url_rows():
    """ticket_url を持つ行を [{tweet_id, ticket_url}] で全件返す(URL修復用)。"""
    out, offset = [], 0
    while True:
        _, rows = _request("GET", "auto_events", params={
            "select": "tweet_id,ticket_url", "ticket_url": "not.is.null",
            "order": "tweet_id.asc", "limit": "1000", "offset": str(offset)})
        rows = rows or []
        out += rows
        if len(rows) < 1000:
            return out
        offset += 1000


def patch_auto_event(tweet_id: str, patch: dict):
    """tweet_id を指定して1行の一部の列だけを書き換える。"""
    _request("PATCH", "auto_events", params={"tweet_id": f"eq.{tweet_id}"},
             body=patch, headers={"Prefer": "return=minimal"})


def upsert_auto_event(row: dict):
    """tweet_id をキーに auto_events を upsert(重複投稿は上書き)。"""
    _drop_missing_columns(_clean_urls([row]))
    _request(
        "POST",
        "auto_events",
        params={"on_conflict": "tweet_id"},
        body=[row],
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


def upsert_auto_events(rows):
    """auto_events を一括 upsert。1行ずつ POST すると件数分のHTTP往復になる
    (フェス1本で150グループ分の行が立つため、ここが実行時間を食っていた)。"""
    rows = _drop_missing_columns(_clean_urls([r for r in rows if r]))
    if not rows:
        return
    for i in range(0, len(rows), 200):
        _request(
            "POST",
            "auto_events",
            params={"on_conflict": "tweet_id"},
            body=rows[i:i + 200],
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )


def get_auto_event_ids(idol_slug: str, prefix: str = "tt:"):
    """指定アイドルの、prefix で始まる tweet_id 一覧を返す(再突合による削除用)。"""
    _, rows = _request(
        "GET",
        "auto_events",
        params={"select": "tweet_id", "idol_slug": f"eq.{idol_slug}", "tweet_id": f"like.{prefix}*"},
    )
    return [r["tweet_id"] for r in (rows or [])]


def get_auto_event_rows_by_prefix(prefix: str):
    """tweet_id が prefix で始まる行を [{tweet_id, idol_slug, event_name}] で全件返す。

    図鑑からリンクを外したグループの行(tt:/gc:/site:)は、そのグループが巡回
    対象から消えるため run_* の再突合では掃除されず残り続ける。孤児の判定に
    使うので、グループ横断で読む必要がある。1000行上限があるので order を
    固定して offset を進める(order を付けないと頁の境目で重複/取り落ちが出る)。
    """
    out = []
    for offset in range(0, 100000, 1000):
        _, rows = _request(
            "GET",
            "auto_events",
            params={
                "select": "tweet_id,idol_slug,event_name",
                "tweet_id": f"like.{prefix}*",
                "order": "tweet_id",
                "limit": 1000,
                "offset": offset,
            },
        )
        rows = rows or []
        out.extend(rows)
        if len(rows) < 1000:
            break
    return out


def delete_auto_events(tweet_ids):
    """tweet_id を指定して auto_events を一括削除(件数分の往復を避ける)。"""
    ids = [t for t in tweet_ids if t]
    if not ids:
        return 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        quoted = ",".join('"' + t.replace('"', '\\"') + '"' for t in chunk)
        _request(
            "DELETE",
            "auto_events",
            params={"tweet_id": f"in.({quoted})"},
            headers={"Prefer": "return=minimal"},
        )
    return len(ids)


def get_upcoming_counts(today: str):
    """今後(today以降)のイベント件数を idol_slug ごとに数えて返す。

    グループ別検索の順番決めに使う。0件か否かではなく件数で見るのは、
    chuLa のように対バン経由で数件だけ拾えている(=0件ではないが実態より
    大幅に少ない)グループを後回しにしないため。1000行上限があるので
    offset を進めて読む。
    """
    import collections
    counts = collections.Counter()
    for offset in range(0, 20000, 1000):
        _, rows = _request(
            "GET",
            "auto_events",
            params={
                "select": "idol_slug",
                "hidden": "eq.false",
                "event_datetime": f"gte.{today}",
                "order": "tweet_id",
                "limit": 1000,
                "offset": offset,
            },
        )
        rows = rows or []
        counts.update(r["idol_slug"] for r in rows if r.get("idol_slug"))
        if len(rows) < 1000:
            break
    return counts


def get_imminent_slugs(today: str, until: str):
    """today〜until に出演予定のある idol_slug の集合を返す。

    巡回の優先度決めに使う。出演時刻(「出演19:35-20:00」)は公演の1〜3日前に
    各グループがカレンダーへ書き込むため、直前のグループを先に読まないと
    タイムテーブルを埋められないまま当日を過ぎてしまう。
    """
    out = set()
    for offset in range(0, 20000, 1000):
        _, rows = _request(
            "GET",
            "auto_events",
            params={
                "select": "idol_slug",
                "hidden": "eq.false",
                # 同じ列に2つの条件を付けるので and=(...) で渡す
                "and": f"(event_datetime.gte.{today},event_datetime.lt.{until})",
                "order": "tweet_id",
                "limit": 1000,
                "offset": offset,
            },
        )
        rows = rows or []
        out.update(r["idol_slug"] for r in rows if r.get("idol_slug"))
        if len(rows) < 1000:
            break
    return out


def delete_auto_event(tweet_id: str):
    """tweet_id を指定して auto_events の1行を削除する。"""
    _request(
        "DELETE",
        "auto_events",
        params={"tweet_id": f"eq.{tweet_id}"},
        headers={"Prefer": "return=minimal"},
    )


def get_last_seen(account: str):
    """worker_state から前回既読ツイートIDを取得(無ければ None)。"""
    _, rows = _request(
        "GET",
        "worker_state",
        params={"select": "last_tweet_id", "account": f"eq.{account}"},
    )
    if rows:
        return rows[0].get("last_tweet_id")
    return None


def get_last_seen_by_prefix(prefix: str):
    """worker_state のうち account が prefix で始まる行を {account: 値} でまとめて返す。

    グループ別検索の順番決めでは378グループ分の値が必要で、1件ずつ引くと
    毎回378リクエストになる。1回で読む。
    """
    out = {}
    for offset in range(0, 20000, 1000):
        _, rows = _request(
            "GET",
            "worker_state",
            params={
                "select": "account,last_tweet_id",
                "account": f"like.{prefix}*",
                "order": "account",
                "limit": 1000,
                "offset": offset,
            },
        )
        rows = rows or []
        for r in rows:
            out[r["account"]] = r.get("last_tweet_id")
        if len(rows) < 1000:
            break
    return out


def set_last_seen_many(pairs):
    """worker_state を一括 upsert。pairs は (account, 値) の並び。"""
    body = [{"account": a, "last_tweet_id": str(v)} for a, v in pairs]
    if not body:
        return
    for i in range(0, len(body), 500):
        _request(
            "POST",
            "worker_state",
            params={"on_conflict": "account"},
            body=body[i:i + 500],
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )


def set_last_seen(account: str, last_tweet_id: str):
    """worker_state を upsert(account 主キー)。"""
    _request(
        "POST",
        "worker_state",
        params={"on_conflict": "account"},
        body=[{"account": account, "last_tweet_id": str(last_tweet_id)}],
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


def get_approved_idol_info():
    """承認済みの有志追記(idol_info)を取得。各 payload に idolSlug と links を含む。"""
    _, rows = _request(
        "GET",
        "shared_items",
        params={"select": "payload", "type": "eq.idol_info", "status": "eq.approved"},
    )
    return [r.get("payload", {}) for r in (rows or [])]


def fetch_url_json(url: str):
    """認証不要の公開JSON(catalog.json など)を取得する。"""
    req = urllib.request.Request(url, headers={"User-Agent": "idollife-worker"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))
