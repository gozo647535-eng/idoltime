"""イープラス(e+)の公演情報を取り込む。

メジャー/大箱系のグループは ticketdive / livepocket / tiget を使わず、
ぴあ・e+・ローソンで売る。そのため Yahoo 経由の発見では届かず、公式サイトの
登録が無いと0件のままだった(CiON・PIGGS 等)。

e+ のアーティストページは JSON-LD の schema.org/Event を持っており、
startDate が開演日時、location.name が会場になっている。LLM は使わない。

ローチケ(l-tike.com)は接続自体が応答なしで落とされるため扱わない。
"""
import datetime
import hashlib
import json
import re
import unicodedata
import urllib.parse

import sites  # 取得(_get)と可視テキスト化を共用
import ticketdive  # 名前の正規化を共用
import timetree  # イベント名の整形・プレースホルダ判定を共用

JST = datetime.timezone(datetime.timedelta(hours=9))
EXTRACT_VERSION = "ep1"

SEARCH_URL = "https://eplus.jp/sf/search?keyword={q}"
WORD_URL = "https://eplus.jp/sf/word/{wid}"
_WORD_RE = re.compile(r"/sf/word/(\d+)")
_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
# 「○○／一般発売」「／プレオーダー受付」など、券種のラベルは公演名ではない
_SALE_SUFFIX = re.compile(r"\s*／.*$")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return ticketdive._norm_name(unicodedata.normalize("NFKC", s or ""))


def _title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    return _WS.sub(" ", m.group(1)).strip() if m else ""


def _artist_of_title(title: str) -> str:
    """「<アーティスト>のチケット、ライブ・コンサート、配信情報 - イープラス」から
    アーティスト名の部分だけを取り出す。"""
    t = re.sub(r"\s*-\s*イープラス\s*$", "", title or "")
    t = re.sub(r"の(?:チケット|公演).*$", "", t)
    return t.strip()


def resolve_page(name: str):
    """グループ名から e+ のアーティストページURLを求める。見つからなければ None。

    検索結果の先頭の /sf/word/ が目的のアーティストとは限らない
    (「CiON」で検索すると ESTACION のページが返る)。ページのタイトルに
    グループ名が入っていることを確かめてから採用する。
    """
    key = _norm(name)
    if len(key) < 3:
        return None
    html = sites._get(SEARCH_URL.format(q=urllib.parse.quote(name)))
    if not html:
        return None
    for wid in dict.fromkeys(_WORD_RE.findall(html)):
        url = WORD_URL.format(wid=wid)
        page = sites._get(url)
        if not page:
            continue
        # タイトル全体への部分一致では足りない。「CiON」は「ESTACION」に
        # 含まれてしまい、別アーティストのページを掴んでいた。
        artist = _norm(_artist_of_title(_title(page)))
        if not artist:
            continue
        if artist == key or (key in artist and len(key) >= len(artist) - 6):
            return url
    return None


def _events_in(html: str):
    out = []
    for block in _LD_RE.findall(html or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for ev in (data if isinstance(data, list) else [data]):
            if isinstance(ev, dict) and ev.get("@type") == "Event":
                out.append(ev)
    return out


def fetch_public_events(page_url: str):
    """アーティストページの今後の公演を (content_hash, [dict]) で返す。

    dict は TimeTree / Google カレンダーと同じ形にそろえる。
    """
    html = sites._get(page_url)
    if not html:
        return None, []
    raw = _events_in(html)
    content_hash = hashlib.sha1(
        (EXTRACT_VERSION + "\n" + page_url + "\n"
         + json.dumps(raw, ensure_ascii=False, sort_keys=True)).encode("utf-8", "ignore")
    ).hexdigest()

    today = datetime.datetime.now(JST).date()
    by_key = {}
    for ev in raw:
        start = (ev.get("startDate") or "").strip()
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?", start)
        if not m:
            continue
        y, mo, d, hh, mi = m.groups()
        try:
            day = datetime.date(int(y), int(mo), int(d))
        except ValueError:
            continue
        if day < today:
            continue
        venue = ((ev.get("location") or {}).get("name") or "").strip() or None
        name = _SALE_SUFFIX.sub("", _WS.sub(" ", (ev.get("name") or "").strip()))
        name = re.sub(r"^[★☆]+", "", name).strip()
        title = timetree._core_name(name) or name
        if not title or timetree.looks_placeholder(title):
            title = None
        # 同じ公演が券種ごとに並ぶので、日付+会場でまとめ、名前のある行を優先する
        k = (str(day), venue)
        cur = by_key.get(k)
        cand = {
            "id": hashlib.sha1(f"{page_url}|{day}|{venue}|{title}".encode("utf-8")).hexdigest()[:20],
            "title": title,
            "event_datetime": f"{day} {hh}:{mi}" if hh else str(day),
            "venue": venue,
            "ticket_url": ev.get("url") or page_url,
            "note": None,
            "page_url": ev.get("url") or page_url,
        }
        if cur is None or (not cur.get("title") and cand["title"]) or \
           (cand["title"] and cur.get("title") and len(cand["title"]) > len(cur["title"])):
            by_key[k] = cand
    # 名前がまったく取れなかった公演は捨てる(券種ラベルだけの行)
    return content_hash, [v for v in by_key.values() if v.get("title")]
