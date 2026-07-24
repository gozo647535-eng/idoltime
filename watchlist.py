"""監視対象アカウントの自動生成。

図鑑(catalog.json)のアイドルのXリンクと、有志が承認済みで追記したXリンク
(Supabase の idol_info)から、監視すべきXハンドル → idol_slug の対応を組み立てる。
accounts.json(手動リスト)があれば、それを優先的に加える。

twikit/Gemini に依存しないため単体テスト可能。
"""
import json
import os
import re
from typing import Optional

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


# SNSではない=公式サイトとみなすドメイン断片(除外リスト)
_SOCIAL_HOSTS = ("x.com", "twitter.com", "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "line.me")


def pick_official_site(links) -> Optional[str]:
    """links から公式サイトURLを選ぶ。label が『公式サイト』を最優先、無ければ最初の非SNS http(s)。"""
    for link in links or []:
        if (link.get("label") or "") == "公式サイト":
            url = link.get("url") or ""
            if url.startswith("http"):
                return url
    for link in links or []:
        url = link.get("url") or ""
        if url.startswith("http") and not any(h in url for h in _SOCIAL_HOSTS):
            return url
    return None


# TimeTree 公開カレンダーとみなすホスト/ラベル
_TIMETREE_HOSTS = ("timetr.ee", "timetreeapp.com")
_TIMETREE_LABELS = ("タイムツリー", "timetree", "time tree")


def pick_timetree_url(links) -> Optional[str]:
    """links から TimeTree 公開カレンダーのURLを選ぶ。ラベル一致 or ホスト一致。"""
    for link in links or []:
        label = (link.get("label") or "").lower()
        url = link.get("url") or ""
        if not url.startswith("http"):
            continue
        if any(l in label for l in _TIMETREE_LABELS):
            return url
        if any(h in url for h in _TIMETREE_HOSTS):
            return url
    return None


def build_timetree_watch_list():
    """図鑑カタログ + 有志追記から、TimeTree 監視対象 [{idol_slug, name, tt_url}] を作る。"""
    seen = set()
    watch = []

    def add(slug, name, links):
        if not slug or slug in seen:
            return
        url = pick_timetree_url(links)
        if url:
            watch.append({"idol_slug": slug, "name": name or slug, "tt_url": url})
            seen.add(slug)

    try:
        catalog = sb.fetch_url_json(CATALOG_URL)
        for idol in catalog.get("idols", []):
            add(idol.get("slug"), idol.get("name"), idol.get("links", []))
    except Exception as e:
        print(f"catalog.json の取得に失敗(TimeTree・スキップ): {e}")

    try:
        for payload in sb.get_approved_idol_info():
            add(payload.get("idolSlug"), payload.get("idolName"), payload.get("links", []))
    except Exception as e:
        print(f"idol_info の取得に失敗(TimeTree・スキップ): {e}")

    return watch


def build_site_watch_list():
    """図鑑カタログ + 有志追記から、公式サイト監視対象 [{idol_slug, name, site_url}] を作る。"""
    seen = set()
    watch = []

    def add(slug, name, links):
        if not slug or slug in seen:
            return
        url = pick_official_site(links)
        if url:
            watch.append({"idol_slug": slug, "name": name or slug, "site_url": url})
            seen.add(slug)

    try:
        catalog = sb.fetch_url_json(CATALOG_URL)
        for idol in catalog.get("idols", []):
            add(idol.get("slug"), idol.get("name"), idol.get("links", []))
    except Exception as e:
        print(f"catalog.json の取得に失敗(サイト監視・スキップ): {e}")

    try:
        for payload in sb.get_approved_idol_info():
            add(payload.get("idolSlug"), payload.get("idolName"), payload.get("links", []))
    except Exception as e:
        print(f"idol_info の取得に失敗(サイト監視・スキップ): {e}")

    return watch
