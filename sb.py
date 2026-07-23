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
