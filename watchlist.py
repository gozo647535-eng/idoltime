"""監視対象アカウントの自動生成。

図鑑(catalog.json)のアイドルのXリンクと、有志が承認済みで追記したXリンク
(Supabase の idol_info)から、監視すべきXハンドル → idol_slug の対応を組み立てる。
accounts.json(手動リスト)があれば、それを優先的に加える。

twikit/Gemini に依存しないため単体テスト可能。
"""
import json
import os
import re

import sb

CATALOG_URL = os.environ.get("CATALOG_URL", "https://idollife.vercel.app/catalog.json")
ACCOUNTS_FILE = "accounts.json"

# https://x.com/handle や https://twitter.com/handle からハンドルを取り出す
_X_RE = re.compile(r"(?:x\.com|twitter\.com)/@?([A-Za-z0-9_]{1,15})", re.IGNORECASE)

# ハンドルとして無効な予約パス
_RESERVED = {"home", "search", "hashtag", "i", "intent", "share", "explore", "messages"}


def extract_x_handle(url):
    if not url:
        return None
    m = _X_RE.search(url)
    if not m:
        return None
    handle = m.group(1)
    if handle.lower() in _RESERVED:
        return None
    return handle


def _add(watch, handle, slug):
    if not handle or not slug:
        return
    key = handle.lower()
    if key not in watch:
        watch[key] = {"handle": handle, "idol_slug": slug}


def _load_manual():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"accounts.json の読み込みに失敗: {e}")
        return []


def build_watch_list():
    """監視対象 [{handle, idol_slug}] を組み立てて返す(ハンドル単位で重複排除)。"""
    watch = {}

    # 1. 手動リスト(任意・最優先)
    for e in _load_manual():
        _add(watch, (e.get("handle") or "").lstrip("@"), e.get("idol_slug"))

    # 2. 図鑑カタログのアイドルのXリンク
    try:
        catalog = sb.fetch_url_json(CATALOG_URL)
        for idol in catalog.get("idols", []):
            slug = idol.get("slug")
            for link in idol.get("links", []):
                _add(watch, extract_x_handle(link.get("url")), slug)
    except Exception as e:
        print(f"catalog.json の取得に失敗(スキップ): {e}")

    # 3. 有志が承認済みで追記したXリンク
    try:
        for payload in sb.get_approved_idol_info():
            slug = payload.get("idolSlug")
            for link in payload.get("links", []) or []:
                _add(watch, extract_x_handle(link.get("url")), slug)
    except Exception as e:
        print(f"idol_info の取得に失敗(スキップ): {e}")

    return list(watch.values())
