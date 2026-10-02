"""Supabase(PostgREST)への入出力。標準ライブラリのみで依存を最小化。

service_role キーで書き込むため、SUPABASE_SERVICE_KEY は絶対に公開しないこと
(GitHub Secrets にのみ置く)。
"""
import json
import os
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


def upsert_auto_event(row: dict):
    """tweet_id をキーに auto_events を upsert(重複投稿は上書き)。"""
    _request(
        "POST",
        "auto_events",
        params={"on_conflict": "tweet_id"},
        body=[row],
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


def get_covered_slugs(today: str):
    """今後(today以降)のイベントが1件以上ある idol_slug の集合を返す。

    グループ別検索の順番決めに使う。1000行上限があるので offset を進めて読む。
    """
    covered = set()
    for offset in range(0, 20000, 1000):
        _, rows = _request(
            "GET",
            "auto_events",
            params={
                "select": "idol_slug",
                "hidden": "eq.false",
                "event_datetime": f"gte.{today}",
                "limit": 1000,
                "offset": offset,
            },
        )
        rows = rows or []
        covered.update(r["idol_slug"] for r in rows if r.get("idol_slug"))
        if len(rows) < 1000:
            break
    return covered


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
