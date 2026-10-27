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

CATALOG_URL = os.environ.get("CATALOG_URL", "https://genba-idol.vercel.app/catalog.json")
ACCOUNTS_FILE = "accounts.json"
# 公式サイト巡回から外すアイドルの slug 一覧(JSONの配列)。
# 対バン中心のグループはチケットサイト発見側で拾えるため、公式サイトを巡回しても
# ほとんど成果が無い。そういうグループをここに列挙して巡回対象から外し、
# 実行時間と LLM 呼び出しを、公式サイトでしか取れないグループに振り向ける。
SITE_SKIP_FILE = "site_skip.json"

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
# カレンダーのURLは公式サイトではない。これを除かないと、TimeTree しか登録が無い
# グループで TimeTree のページを「公式サイト」として巡回してしまい、
# LLM の枠を無駄に使う(実測15組が該当していた)。
_CALENDAR_HOSTS = ("timetr.ee", "timetreeapp.com", "calendar.google.com")


def pick_official_site(links) -> Optional[str]:
    """links から公式サイトURLを選ぶ。label が『公式サイト』を最優先、無ければ最初の非SNS http(s)。"""
    for link in links or []:
        if (link.get("label") or "") == "公式サイト":
            url = link.get("url") or ""
            if url.startswith("http"):
                return url
    for link in links or []:
        url = link.get("url") or ""
        if (url.startswith("http")
                and not any(h in url for h in _SOCIAL_HOSTS)
                and not any(h in url for h in _CALENDAR_HOSTS)):
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


# Google カレンダーとみなすホスト/ラベル
_GCAL_HOSTS = ("calendar.google.com",)
_GCAL_LABELS = ("googleカレンダー", "google カレンダー", "google calendar", "gカレンダー")


def pick_gcal_url(links) -> Optional[str]:
    """links から Google 公開カレンダーのURLを選ぶ。

    「予定を追加」用の render?action=TEMPLATE はカレンダーではないので採らない。
    """
    for link in links or []:
        label = (link.get("label") or "").lower()
        url = link.get("url") or ""
        if not url.startswith("http"):
            continue
        if not (any(h in url for h in _GCAL_HOSTS) or any(l in label for l in _GCAL_LABELS)):
            continue
        if "action=TEMPLATE" in url.replace("&amp;", "&"):
            continue
        return url
    return None


def build_gcal_watch_list():
    """図鑑カタログ + 有志追記から、Google カレンダー監視対象
    [{idol_slug, name, gc_url}] を作る。"""
    seen = set()
    watch = []

    def add(slug, name, links):
        if not slug or slug in seen:
            return
        url = pick_gcal_url(links)
        if url:
            watch.append({"idol_slug": slug, "name": name or slug, "gc_url": url})
            seen.add(slug)

    try:
        catalog = sb.fetch_url_json(CATALOG_URL)
        for idol in catalog.get("idols", []):
            add(idol.get("slug"), idol.get("name"), idol.get("links", []))
    except Exception as e:
        print(f"catalog.json の取得に失敗(Googleカレンダー・スキップ): {e}")

    try:
        for payload in sb.get_approved_idol_info():
            add(payload.get("idolSlug"), payload.get("idolName"), payload.get("links", []))
    except Exception as e:
        print(f"idol_info の取得に失敗(Googleカレンダー・スキップ): {e}")

    watch += _auto_found("gcfound:", seen, "gc_url",
                         lambda v: f"https://calendar.google.com/calendar/embed?src={v}",
                         "公式サイトのGoogleカレンダー")
    return watch


def _auto_found(prefix: str, seen: set, url_key: str, to_url, label: str):
    """ワーカーが公式サイトから見つけたカレンダーを監視対象の形にして返す。

    図鑑に登録があるグループは上で seen に入っているので、ここでは足さない
    (図鑑の登録を優先する)。値が "-" の行は「使わない」印として扱う。
    """
    out = []
    try:
        names = {i["idol_slug"]: i["name"] for i in build_idol_name_list()}
        for key, value in sb.get_last_seen_by_prefix(prefix).items():
            slug = key[len(prefix):]
            if not value or value == "-" or slug in seen or slug not in names:
                continue
            out.append({"idol_slug": slug, "name": names[slug], url_key: to_url(value)})
            seen.add(slug)
        if out:
            print(f"自動で見つけた{label}: {len(out)} 件を監視対象に追加")
    except Exception as e:
        print(f"自動発見した{label}の読み込みに失敗(スキップ): {e}")
    return out


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

    watch += _auto_found("ttfound:", seen, "tt_url",
                         lambda v: f"https://timetreeapp.com/public_calendars/{v}",
                         "公式サイトのTimeTree")
    return watch


def _load_site_skip():
    """公式サイト巡回から外す slug の集合を返す。ファイルが無ければ空集合。"""
    try:
        with open(SITE_SKIP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return set()
    except Exception as e:
        print(f"{SITE_SKIP_FILE} の読み込みに失敗(全件巡回します): {e}")
        return set()
    if isinstance(data, dict):  # {"skip": [...]} 形式も許容
        data = data.get("skip", [])
    return {s for s in data if isinstance(s, str)}


def site_skip_slugs():
    """公式サイト巡回から外している slug の集合(孤児行の掃除で使う)。

    巡回を外しただけのグループは、過去に取れた行を消したくないので
    「リンクが無くなった」扱いにはしない。
    """
    return _load_site_skip()


def build_site_watch_list(include_skipped: bool = False):
    """図鑑カタログ + 有志追記から、公式サイト監視対象 [{idol_slug, name, site_url}] を作る。
    site_skip.json に挙げた slug は除外する(include_skipped=True なら除外しない。
    特定グループだけを指定して動作を確かめるときに使う)。"""
    seen = set()
    watch = []
    skip = set() if include_skipped else _load_site_skip()
    skipped = []

    def add(slug, name, links):
        if not slug or slug in seen:
            return
        if slug in skip:
            seen.add(slug)
            skipped.append(slug)
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

    if skipped:
        print(f"公式サイト巡回から除外: {len(skipped)} 件(site_skip.json)")
    # site_skip.json の slug は上で seen に入っているので、自動発見分でも足されない
    watch += _auto_found("sitefound:", seen, "site_url", lambda v: v, "投稿からの公式サイト")
    return watch


def build_idol_name_list():
    """図鑑カタログ + 有志追記から、全登録アイドルの [{idol_slug, name}] を作る。

    公式サイト/TimeTree リンクの有無に関係なく全件返す。TicketDive の出演者一覧と
    名前マッチして「このイベントにうちの登録グループが出るか」を判定するために使う。
    """
    seen = set()
    out = []

    def add(slug, name):
        if not slug or not name or slug in seen:
            return
        out.append({"idol_slug": slug, "name": name})
        seen.add(slug)

    try:
        catalog = sb.fetch_url_json(CATALOG_URL)
        for idol in catalog.get("idols", []):
            add(idol.get("slug"), idol.get("name"))
    except Exception as e:
        print(f"catalog.json の取得に失敗(名前リスト・スキップ): {e}")

    try:
        for payload in sb.get_approved_idol_info():
            add(payload.get("idolSlug"), payload.get("idolName"))
    except Exception as e:
        print(f"idol_info の取得に失敗(名前リスト・スキップ): {e}")

    return out
