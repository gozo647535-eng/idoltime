"""リリースイベント(店舗のインストアイベント)を集める。

ららぽーと・タワーレコード・ヴィレッジヴァンガードのような場所で開かれる
発売記念イベントを、女性アイドルに限らずそのまま拾う。

取り方は3通り:
  - タワーレコード: 店舗イベント一覧が素のHTMLの表。1ページ20件 × 14ページ
    (実測 266件)。日付・時刻・イベント名・アーティスト・内容・店舗が列で並ぶ。
  - HMV: 同じく店舗イベント一覧。ページは Shift_JIS。
  - ヴィレッジヴァンガード: 「リリイベ」カテゴリがある。一覧はJS描画なので
    ticketsite のブラウザを借りて読む。

このファイルは「読む」ところまでを受け持ち、図鑑との突き合わせや
Supabase への書き込みは worker 側で行う。
"""

from __future__ import annotations

import html as _html
import re
import time
import urllib.error
import urllib.request
from typing import Optional

import ticketsite

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
      " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_MIN_INTERVAL = 1.0
_last_call = [0.0]


def _pace() -> None:
    dt = time.time() - _last_call[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_call[0] = time.time()


def _get(url: str, encoding: Optional[str] = None) -> Optional[str]:
    """ページを取る。文字コードは指定が無ければ応答ヘッダ→UTF-8の順で見る。"""
    _pace()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=40) as res:
            raw = res.read()
            enc = encoding or res.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"    リリイベ取得失敗 {url[:70]}: {e}")
        return None
    return raw.decode(enc, errors="replace")


def _text(s: str) -> str:
    """タグを落として前後を整える。"""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ---------------------------------------------------------------- 整理番号

# 「整理番号」「優先入場券」など、入場順が決まる仕組みがあることを示す言葉
_NUMBERED_RE = re.compile(r"整理番号|整理券|優先入場|入場整理|入場順")
# 当日の店頭配布ではなく、事前に売り出す・受け付けることを示す言葉。
# 「事前」だけだと「事前にご確認ください」まで拾ってしまうので、後ろに
# 販売・受付・応募・予約・発券が続くものに限る。
_ADVANCE_RE = re.compile(
    r"事前(?:販売|受付|応募|予約|発券|購入)|前売|"
    r"(?:WEB|ウェブ|オンライン|ネット)(?:にて)?(?:販売|受付|発券|申込)|抽選受付"
)
# LINEアプリで整理券を出す店舗イベントが実際に多い。当日その場ではなく
# 先に動く必要があるので、当日配布とは分けて見せる。
_LINE_RE = re.compile(r"LINE\s*アプリ|LINEアプリ|LINE公式アカウント")


def numbering(text: str) -> dict:
    """整理番号まわりの読み取り。

    返り値 {"numbered": bool, "advance": bool, "line": bool, "lines": [該当行]}。
    lines は画面にそのまま出せるよう、該当した行だけを短く持つ。
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    hit = [l for l in lines if _NUMBERED_RE.search(l)]
    return {
        "numbered": bool(hit),
        "advance": any(_ADVANCE_RE.search(l) for l in hit),
        "line": any(_LINE_RE.search(l) for l in hit),
        "lines": [l[:120] for l in hit[:4]],
    }


# 事前に整理番号や参加券を売るチケットサイト。見つけたら発売日時を読みに行く。
_TICKET_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:livepocket\.jp/e/[\w\-]+|tiget\.net/events/\d+"
    r"|t-dv\.com/[\w\-]+|ticketdive\.com/event/[\w\-]+|eplus\.jp/[\w\-/]+"
    r"|passmarket\.yahoo\.co\.jp/event/show/detail/[\w\-]+)"
)


def ticket_links(page_html: str) -> list:
    """ページ内のチケット販売ページへのリンク(重複なし)。"""
    return list(dict.fromkeys(_TICKET_LINK_RE.findall(page_html or "")))


# ------------------------------------------------------------ タワーレコード

TOWER_LIST = "https://tower.jp/store/event"


def _tower_rows(page_html: str) -> list:
    if "tableModel-recBase1" not in page_html:
        return []
    body = page_html.split('<table class="tableModel-recBase1">', 1)[1].split("</table>", 1)[0]
    out = []
    for tr in re.split(r"<tr>", body)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cells) < 5:
            continue  # 見出し行
        link = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', cells[2], re.S)
        if not link:
            continue
        store = re.search(r'<a href="[^"]*"[^>]*>(.*?)</a>', cells[4], re.S)
        out.append({
            "source": "tower",
            "url": "https://tower.jp" + link.group(1),
            "date": _text(cells[0]).replace("/", "-"),
            "time": _text(cells[1]),
            "name": _text(link.group(2)),
            # 見出し(h3)を取り除いた残りがアーティスト名
            "artist": _text(re.sub(r"<h3.*?</h3>", "", cells[2], flags=re.S)),
            "kind": _text(cells[3]),
            "store": _text(store.group(1)) if store else "",
        })
    return out


def tower_listing(pages: int = 5) -> list:
    """タワーレコードの店舗イベント一覧(開催日の近い順)。"""
    out = []
    for page in range(1, max(1, pages) + 1):
        url = TOWER_LIST + (f"?page={page}" if page > 1 else "")
        page_html = _get(url)
        if not page_html:
            break
        rows = _tower_rows(page_html)
        if not rows:
            print(f"    タワーレコードの一覧に行がありません({len(page_html)}字): {url}")
            break
        out += rows
    return out


# ------------------------------------------------------------------- HMV

HMV_LIST = "https://www.hmv.co.jp/store/event/"


def hmv_listing(pages: int = 3) -> list:
    """HMVの店舗イベント一覧。ページは Shift_JIS。"""
    out = []
    for page in range(1, max(1, pages) + 1):
        url = HMV_LIST + (f"?page={page}" if page > 1 else "")
        page_html = _get(url, encoding="cp932")
        if not page_html:
            break
        rows = _hmv_rows(page_html)
        if not rows:
            print(f"    HMVの一覧に行がありません({len(page_html)}字): {url}")
            break
        out += rows
    return out


# 一覧の本体。
# 「<dt><a href="/store/event/56050/">植村あかり 特典会</a></dt>
#  <dd>2026/10/25 (日) 12:00</dd>
#  <dd class="eventLocation"><a ...>HMV&BOOKS SHIBUYA</a></dd>」
# リンク文字は素のテキストなので [^<]* で止める。.*? にすると、1つ前の
# 「詳細」ボタンのリンクから始まってこの項目の </a></dt> まで伸びてしまい、
# イベントIDと名前がずれる(アーティスト名が二重に入る形で現れた)。
_HMV_MAIN = re.compile(
    r'<dt[^>]*>\s*<a href="(/store/event/\d+/)"[^>]*>([^<]*)</a>\s*</dt>\s*'
    r'<dd>((?:(?!</dd>).)*?)</dd>\s*'
    r'<dd class="eventLocation">((?:(?!</dd>).)*?)</dd>',
    re.S,
)
_HMV_REC = re.compile(
    r'<dt>\s*<a href="(/store/event/\d+/)"[^>]*>([^<]*)</a>\s*</dt>\s*'
    r'<dd>\s*<p class="eventDate">(.*?)</p>',
    re.S,
)


def _hmv_when(head: str):
    d = re.search(r"(20\d\d)/(\d{1,2})/(\d{1,2})", head)
    t = re.search(r"(\d{1,2}):(\d{2})", head)
    return (f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else "",
            f"{int(t.group(1)):02d}:{t.group(2)}" if t else "")


# 「<dl class="idxHeadBox"><dt>アルテミスの翼</dt>」…この中の項目はすべて
# このアーティストのもの。一覧の行にはアーティスト名が入らないのでここから採る。
_HMV_HEAD = re.compile(r'<dl class="idxHeadBox">\s*<dt>(.*?)</dt>', re.S)


def _hmv_artists(page_html: str) -> dict:
    """イベントURL → アーティスト名。idxHeadBox の区切りごとに割り当てる。"""
    out = {}
    parts = re.split(r'<dl class="idxHeadBox">', page_html)
    for part in parts[1:]:
        head = re.match(r"\s*<dt>(.*?)</dt>", part, re.S)
        name = _text(head.group(1)) if head else ""
        if not name:
            continue
        for href in re.findall(r'href="(/store/event/\d+/)"', part):
            out.setdefault(href, name)
    return out


def _hmv_rows(page_html: str) -> list:
    out, seen = [], set()
    artists = _hmv_artists(page_html)

    def add(href, label, date, time_, store):
        name = _text(label)
        if href in seen or not name:
            return
        seen.add(href)
        out.append({
            "source": "hmv",
            "url": "https://www.hmv.co.jp" + href,
            "date": date, "time": time_, "name": name,
            "artist": artists.get(href, ""), "kind": "", "store": _text(store),
        })

    for href, label, when, place in _HMV_MAIN.findall(page_html):
        d, t = _hmv_when(_text(when))
        if not d:
            continue  # 日付の入らない <dd> は別項目。取り違えを避ける
        add(href, label, d, t, place)
    for href, label, when in _HMV_REC.findall(page_html):
        parts = [_text(x) for x in re.split(r"<br\s*/?>", when)]
        d, t = _hmv_when(parts[0] if parts else "")
        add(href, label, d, t, parts[1] if len(parts) > 1 else "")
    return out


# -------------------------------------------------- ヴィレッジヴァンガード

VV_LIST = "https://www.village-v.co.jp/event/"


def vv_listing() -> list:
    """ヴィレッジヴァンガードの「リリイベ」一覧。JS描画なのでブラウザで開く。"""
    page_html = ticketsite.render(VV_LIST, wait_ms=14000, wait_for="/event/0")
    if not page_html:
        print("    ヴィレヴァンの一覧を描画できませんでした")
        return []
    out, seen = [], set()
    # リンクは相対パス(/event/031419/)で、画像だけのアンカーと
    # 題名のアンカーが同じ先を指すので、題名のあるほうを採る
    for href, inner in re.findall(
        r'<a[^>]+href="([^"]*?/event/\d+/)"[^>]*>(.*?)</a>', page_html, re.S
    ):
        name = _text(inner)
        if not name or href in seen:
            continue
        seen.add(href)
        out.append({
            "source": "vv",
            "url": "https://www.village-v.co.jp" + href if href.startswith("/") else href,
            "date": "", "time": "", "name": name,
            "artist": "", "kind": "", "store": "",
        })
    if not out:
        print(f"    ヴィレヴァンの一覧にイベントがありません({len(page_html)}字)")
    return out


# -------------------------------------------------------------- 詳細ページ

# 詳細ページの見出し。長いものから順に見る(「出演者名」のような
# フォームの項目名を「出演」と取り違えないため)。
_LABELS = {
    "when": ["開催日時", "イベント日時", "開催日", "日時"],
    "place": ["開催場所", "開催店舗", "会場", "場所"],
    "cast": ["出演者", "出演", "アーティスト"],
}

# 「2026年10月15日(木) 19:00」「2026/10/15 19:00」のどちらの書き方も拾う
_DATE_RE = re.compile(r"(20\d\d)[年/\-](\d{1,2})[月/\-](\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2})\s*[:時]\s*(\d{2})")


def _field(lines, labels, validate, score=None):
    """見出しの行を探し、同じ行の続き→次の行、の順で中身を返す。

    見出しと中身のあいだに区切り(空白・コロン)があることを必須にしている。
    これが無いと「アーティスト名順:A→Z」の並べ替えボタンを「アーティスト」の
    見出しと読み、中身を「名順:A→Z」と取ってしまう。中身の形(日付が入って
    いるか、2文字以上か)も見て、取り違えたものは次の候補へ送る。

    同じ見出しがページに何度も出ることがあるため(本文の【日時】は日付だけ、
    下の「開催日時」は時刻まで入っている、など)、候補を全部集めてから
    score の高いものを採る。score を渡さなければ最初の候補。
    """
    pat = re.compile(
        r"^[【\[]?(?:" + "|".join(labels) + r")[】\]]?(?:[\s:：]+(?P<rest>.+))?$"
    )
    found = []
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        rest = (m.group("rest") or "").strip()
        if rest:
            if validate(rest):
                found.append(rest)
            continue  # 見出し違い。次の候補へ
        if i + 1 < len(lines) and validate(lines[i + 1]):
            found.append(lines[i + 1])
    if not found:
        return None
    if score is None:
        return found[0]
    return max(found, key=score)


def parse_when(text: str):
    """日付と開始時刻を (YYYY-MM-DD, 'HH:MM'|None) で返す。読めなければ (None, None)。"""
    d = _DATE_RE.search(text or "")
    if not d:
        return None, None
    date = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"
    t = _TIME_RE.search(text[d.end():] if d else "")
    hm = f"{int(t.group(1)):02d}:{t.group(2)}" if t else None
    return date, hm


def fetch_detail(url: str, render: bool = False) -> Optional[dict]:
    """詳細ページを読み、日時・会場・出演・整理番号・チケットリンクを返す。"""
    if render:
        page_html = ticketsite.render(url, wait_ms=12000, wait_for="イベント日時")
    else:
        page_html = _get(url, encoding="cp932" if "hmv.co.jp" in url else None)
    if not page_html:
        return None
    plain = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html, flags=re.S)
    plain = _html.unescape(re.sub(r"<[^>]+>", "\n", plain))
    lines = [re.sub(r"[ \t　]+", " ", l).strip() for l in plain.splitlines()]
    lines = [l for l in lines if l]
    body = "\n".join(lines)

    got = {
        # 日付だけの行より、時刻まで入っている行を優先する
        "when": _field(lines, _LABELS["when"], lambda t: bool(_DATE_RE.search(t)),
                       score=lambda t: 1 if _TIME_RE.search(t) else 0),
        "place": _field(lines, _LABELS["place"], lambda t: len(t) >= 2),
        "cast": _field(lines, _LABELS["cast"], lambda t: len(t) >= 2),
    }

    date, hm = parse_when(got.get("when") or "")
    return {
        "url": url,
        "when_text": got.get("when"),
        "date": date,
        "time": hm,
        "place": got.get("place"),
        "cast": got.get("cast"),
        "numbering": numbering(body),
        "ticket_urls": ticket_links(page_html),
        "body": body[:4000],
    }
