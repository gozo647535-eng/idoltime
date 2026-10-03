"""Yahoo! リアルタイム検索から TicketDive イベントを発見する。

TicketDive は公開の検索API・イベント一覧・sitemap をいずれも持たないため、
公式サイトや TimeTree にリンクが現れない限り「このグループのイベント」を能動的に
探すことができない。一方 Yahoo! リアルタイム検索は X の公開投稿をサーバ描画の
JSON(__NEXT_DATA__)として返すので、そこから TicketDive の URL を拾えば、
既存の高精度パーサ(ticketdive.fetch_ticketdive)に渡して解析できる。

方針: anti-bot の回避は一切行わない。誰にでも配信される公開ページを、通常の
User-Agent で、1日1回・少数クエリだけ、間隔を空けて取得する。
"""
import gzip
import json
import re
import time
import urllib.parse
import urllib.request
import unicodedata
from typing import Dict, List

import ticketsite

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SEARCH_URL = "https://search.yahoo.co.jp/realtime/search?p={q}"

# 連続アクセスを避ける(行儀よく)
_MIN_INTERVAL = 3.0
_last_call = [0.0]

# TicketDive のイベントURL(短縮 t-dv.com も同じイベントへリダイレクトする)
TICKETDIVE_RE = re.compile(
    r"https?://(?:ticketdive\.com/event/[A-Za-z0-9_\-]+|t-dv\.com/[A-Za-z0-9_\-]+)"
)

# 収集対象のチケットサイト。値は URL の正規表現。
SITE_PATTERNS = {
    "ticketdive": TICKETDIVE_RE,
    "livepocket": ticketsite.LIVEPOCKET_RE,
    "tiget": ticketsite.TIGET_RE,
}


# 検索語に使えない装飾記号。Yahoo はこれを含むと0件になる
# (「お願い‼︎フルハウス」の ‼︎ は U+203C + 異体字セレクタ U+FE0E で、実測0投稿。
#  記号を落とすと3投稿返る)。
_DECOR_RE = re.compile(
    "[\uFE00-\uFE0F\u200B-\u200D\u2060]"            # 異体字セレクタ・ゼロ幅
    "|[\u2000-\u206F\u2190-\u2BFF\u3001-\u303F]"    # 約物・矢印・記号
    "|[\uFF01-\uFF0F\uFF1A-\uFF20\uFF3B-\uFF40\uFF5B-\uFF65]"  # 全角記号
    "|[!-/:-@\\[-`{-~]"                                 # 半角記号
)


def search_query_for_name(name: str, suffix: str = "チケット") -> str:
    """グループ名を検索語にする。装飾記号を落とし、キーワードを添える。

    名前だけで引くと40投稿の枠がファンの雑談で埋まりチケットURLが出てこない
    (実測: 名前のみ 0〜2件 / 「名前 チケット」 1〜9件)。
    """
    base = _DECOR_RE.sub(" ", unicodedata.normalize("NFKC", name or ""))
    base = re.sub(r"\s+", " ", base).strip()
    # 記号を落とすと「≠ME」→「ME」、「22/7」→「22 7」のように、検索語として
    # 機能しない短い断片になる名前がある。無駄な1リクエストになるので捨てる。
    core = base.replace(" ", "")
    # 日本語は3文字でも十分に特定的(風男塾・江戸桜)。英数字だけの名前は
    # 他の語に紛れるので4文字以上を求める。
    min_len = 3 if any(not c.isascii() for c in core) else 4
    if len(core) < min_len or core.isdigit():
        return ""
    return f"{base} {suffix}".strip()


def _throttle():
    dt = time.time() - _last_call[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_call[0] = time.time()


def _get(url: str, timeout: int = 25):
    try:
        _throttle()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Encoding": "gzip",
            "Accept-Language": "ja,en;q=0.8",
        })
        r = urllib.request.urlopen(req, timeout=timeout)
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")
    except Exception as e:
        print(f"    Yahooリアルタイム取得失敗 {url}: {e}")
        return None


def search_posts(query: str) -> List[dict]:
    """クエリに一致する X の公開投稿(構造化済み)を返す。失敗時は []。"""
    html = _get(SEARCH_URL.format(q=urllib.parse.quote(query)))
    if not html:
        return []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        print("    Yahooリアルタイム: __NEXT_DATA__ が見つかりません(仕様変更の可能性)")
        return []
    try:
        data = json.loads(m.group(1))
        entries = data["props"]["pageProps"]["pageData"]["timeline"]["entry"]
    except Exception as e:
        print(f"    Yahooリアルタイム: 構造の解析に失敗: {e}")
        return []
    return entries if isinstance(entries, list) else []


def _urls_in_post(e: dict) -> List[str]:
    """投稿に含まれるURLを集める。displayUrl は表示用に末尾が省略される
    (例: ticketdive.com/event/moment7_…)ので使わない。"""
    out = []
    for u in (e.get("urls") or []):
        for k in ("expandedUrl", "url"):
            v = u.get(k)
            if v:
                out.append(v if v.startswith("http") else "https://" + v)
    body = e.get("displayTextBody") or e.get("displayText") or ""
    out.extend(TICKETDIVE_RE.findall(body))
    return out


def collect_ticketdive_urls(queries: List[str], limit: int = 60) -> List[str]:
    """複数クエリを検索し、投稿中の TicketDive URL を重複なく集めて返す。"""
    found: Dict[str, None] = {}
    seen_keys = set()
    for q in queries:
        posts = search_posts(q)
        print(f"  Yahooリアルタイム「{q}」: {len(posts)} 投稿")
        for e in posts:
            for u in _urls_in_post(e):
                m = TICKETDIVE_RE.match(u)
                if not m:
                    continue
                clean = m.group(0)  # ?_gl=... 等の追跡パラメータを落とす
                key = clean.rstrip("/").split("/")[-1]  # 短縮/正規どちらでも同じイベント識別子
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                found[clean] = None
                if len(found) >= limit:
                    print(f"  上限 {limit} 件に達したため打ち切り")
                    return list(found)
    return list(found)


def collect_event_urls(queries: List[str], limit_per_site: int = 60,
                       other_urls: Dict[str, List[str]] = None) -> Dict[str, List[str]]:
    """複数クエリを検索し、チケットサイトごとにイベントURLを重複なく集めて返す。

    同一イベントを指す短縮URLと正規URLは、末尾の識別子で重複排除する。
    other_urls を渡すと、チケットサイト以外のURLもクエリごとに記録する
    (グループの公式サイトを見つけるのに使う。検索を増やさずに済む)。
    """
    found: Dict[str, Dict[str, None]] = {k: {} for k in SITE_PATTERNS}
    seen_keys: Dict[str, set] = {k: set() for k in SITE_PATTERNS}
    for q in queries:
        posts = search_posts(q)
        hits = 0
        for e in posts:
            for u in _urls_in_post(e):
                if other_urls is not None and not any(
                        p.match(u) for p in SITE_PATTERNS.values()):
                    other_urls.setdefault(q, [])
                    if u not in other_urls[q]:
                        other_urls[q].append(u)
                for site, pat in SITE_PATTERNS.items():
                    m = pat.match(u)
                    if not m:
                        continue
                    clean = m.group(0)  # 追跡パラメータを落とす
                    key = clean.rstrip("/").split("/")[-1]
                    if key in seen_keys[site] or len(found[site]) >= limit_per_site:
                        continue
                    seen_keys[site].add(key)
                    found[site][clean] = None
                    hits += 1
                    break
        print(f"  Yahooリアルタイム「{q}」: {len(posts)} 投稿 / 新規 {hits} 件")
    return {k: list(v) for k, v in found.items()}
