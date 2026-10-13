"""LivePocket / TiGET のイベントページを構造化して読む。

どちらも schema.org の JSON-LD を埋めているが、素直に扱えるのは LivePocket だけ:

- LivePocket: 正しい JSON。performer に出演者が入り、HTML 側にも出演者欄がある。
- TiGET: JSON 内に // コメントが混ざり、文字列に生の改行が入るため json.loads は通らない。
  日付も "2026年10月11日(日)T18:30:+0900" という独自形式。さらに performer は
  Ruby のオブジェクトダンプ("#<Performer:0x...>")で使い物にならないので、
  出演者は description の「出演」欄から取る。

そこでサイト差をこのモジュールで吸収し、同じ形の dict を返す。
"""
import gzip
import html as _html
import re
import time
import urllib.request
from typing import List, Optional

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

LIVEPOCKET_RE = re.compile(r"https?://(?:t\.)?livepocket\.jp/e/[A-Za-z0-9_\-]+")
TIGET_RE = re.compile(r"https?://tiget\.net/events/[0-9]+")


def site_of(url: str) -> Optional[str]:
    if not url:
        return None
    if LIVEPOCKET_RE.match(url):
        return "livepocket"
    if TIGET_RE.match(url):
        return "tiget"
    return None


# 相手サイトに負荷をかけないよう取得間隔を空ける
_MIN_INTERVAL = 1.5
_last_call = [0.0]


def _pace() -> None:
    dt = time.time() - _last_call[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_call[0] = time.time()


# AWS WAF のボット対策ページの目印。本物のページは数十KBあるのに対し、
# このページは2KBほどで中身が無く、JavaScript の認証だけを返す。
_WALL_MARKS = ("awsWafCookieDomainList", "gokuProps", "challenge.js")


def bot_wall(html: Optional[str]) -> bool:
    if not html:
        return False
    return len(html) < 20000 and any(m in html for m in _WALL_MARKS)


class _Browser:
    """ボット対策が求める「JavaScript を実行する本物のブラウザ」として開く。

    LivePocket はデータセンターからの素の HTTP 取得に認証ページを返す。
    求められているのはブラウザであることなので、Chromium で素直に開く。
    IP の切り替えや突破代行サービスのような、検知自体をかいくぐる手段は使わない。

    1回の実行で Chromium は1つだけ起動し、認証済みのクッキーを以降の
    ページでも使い回す(認証のやり直しを増やさないため)。
    取得件数も _MAX_PAGES までに抑える。
    """

    _MAX_PAGES = 30

    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None
        self._broken = False
        self.used = 0

    def _ensure_page(self):
        if self._broken or self.used >= self._MAX_PAGES:
            return None
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = self._browser.new_context(
                user_agent=UA, locale="ja-JP",
                viewport={"width": 1280, "height": 900})
            self._page = ctx.new_page()
            print("    ブラウザを起動しました(ボット対策ページの迂回用)")
        except Exception as e:
            print(f"    ブラウザを起動できません: {type(e).__name__} {e}")
            self._broken = True
            self.close()
        return self._page

    def get(self, url: str, timeout: int = 30000) -> Optional[str]:
        page = self._ensure_page()
        if page is None:
            return None
        self.used += 1
        _pace()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            html = page.content()
            # 認証ページが返ったら、ブラウザ側の JavaScript が済むのを待つ。
            # 認証は通常1回で終わり、同じクッキーが以降のページでも使える。
            for _ in range(3):
                if not bot_wall(html):
                    break
                page.wait_for_timeout(3500)
                html = page.content()
                if bot_wall(html):
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=timeout)
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    html = page.content()
            if bot_wall(html):
                print(f"    ボット対策を通過できません: {url[:70]}")
                return None
            return html
        except Exception as e:
            print(f"    ブラウザでの取得に失敗: {type(e).__name__} {str(e)[:70]} / {url[:60]}")
            return None

    def close(self) -> None:
        for obj in (self._browser, self._pw):
            try:
                if obj is self._pw:
                    obj and obj.stop()
                else:
                    obj and obj.close()
            except Exception:
                pass
        self._pw = self._browser = self._page = None


_BROWSER = _Browser()


def close_browser() -> None:
    """起動していればブラウザを閉じる(1回の実行の最後に呼ぶ)。"""
    _BROWSER.close()


def _http_get(url: str, timeout: int = 20) -> Optional[str]:
    try:
        _pace()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Encoding": "gzip", "Accept-Language": "ja,en;q=0.8"})
        r = urllib.request.urlopen(req, timeout=timeout)
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")
    except Exception as e:
        print(f"    チケットサイト取得失敗 {url}: {e}")
        return None


def _get(url: str, timeout: int = 20) -> Optional[str]:
    """ページを取る。ボット対策ページが返ったときだけブラウザで開き直す。"""
    html = _http_get(url, timeout)
    if bot_wall(html):
        return _BROWSER.get(url)
    return html


def _ldjson_block(html: str) -> str:
    m = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S)
    return m.group(1) if m else ""


def _json_string(block: str, key: str) -> Optional[str]:
    """壊れたJSONでも "key": "..." の値を取り出す。
    エスケープを尊重しつつ閉じ引用符まで走査するので、値に生の改行が入っていても読める。"""
    m = re.search(r'"%s"\s*:\s*"' % re.escape(key), block)
    if not m:
        return None
    i = m.end()
    out = []
    while i < len(block):
        c = block[i]
        if c == "\\" and i + 1 < len(block):
            nxt = block[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            break
        out.append(c)
        i += 1
    return "".join(out).strip() or None


def _nested_name(block: str, section: str) -> Optional[str]:
    """"section": { ... "name": "..." } の name を取り出す。"""
    m = re.search(r'"%s"\s*:\s*\{' % re.escape(section), block)
    if not m:
        return None
    return _json_string(block[m.end():m.end() + 1200], "name")


def _jp_datetime(s: str) -> Optional[str]:
    """'2026年10月11日(日)T18:30:+0900' / ISO を 'YYYY-MM-DD HH:MM' に。時刻が無ければ日付のみ。"""
    if not s:
        return None
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s) or re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    tm = re.search(r"T(\d{1,2}):(\d{2})", s)
    if tm and (tm.group(1) != "00" or tm.group(2) != "00"):
        return f"{date} {int(tm.group(1)):02d}:{tm.group(2)}"
    return date


def _sale_datetime(s: str) -> Optional[str]:
    """'2026-09-15T19:54:+0900' → 'YYYY-MM-DD HH:MM:SS'。"""
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    tm = re.search(r"T(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    hh, mm, ss = (tm.group(1), tm.group(2), tm.group(3) or "00") if tm else ("00", "00", "00")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {int(hh):02d}:{mm}:{ss}"


# description 中の出演者欄。「■出演：」「出演/LINE UP」等のラベルから次の見出しまで。
_ACT_LABEL = re.compile(
    r"(?:^|\n)\s*[■◆●・\[【]?\s*(?:出演(?:者|アーティスト)?|LINE\s?UP|LINEUP|ACT|アクト)\s*[】\]]?\s*[:：]?\s*",
    re.IGNORECASE)
_NEXT_HEADING = re.compile(r"\n\s*[■◆●\[【]|\n\s*※|\n\s*\n")


def _act_text(description: str):
    """description から出演者欄を抜き、(テキスト, ラベルが見つかったか) を返す。

    ラベルが無いまま全文を返した場合は labeled=False。呼び出し側はそのとき
    部分一致での照合を控える(注意書き等に名前が出ただけで誤検出するため)。
    """
    if not description:
        return "", False
    m = _ACT_LABEL.search(description)
    if not m:
        return description, False
    rest = description[m.end():]
    nx = _NEXT_HEADING.search(rest)
    seg = rest[: nx.start()] if nx else rest
    seg = seg.strip()
    return (seg, True) if seg else (description, False)


# 出演者欄に紛れ込む注意書き・料金・時刻などを弾くための特徴
_NOT_A_NAME = re.compile(
    r"(?:[¥￥]|\d+\s*円|https?://|[@＠]\w|[※⚠]|[【】<>]|お問い?合わせ|受付|予約|決済|"
    r"キャンセル|変更|入場|整理番号|ドリンク|物販|特典|開場|開演|終演|公式|タイトル|開催日|"
    r"OPEN|START|DOOR|ADV|href)",
    re.IGNORECASE)


def _split_performers(s: str) -> List[str]:
    """出演者欄を名前らしい単位に割る。自由文なので、明らかに名前でない行は捨てる。"""
    out = []
    # description には HTML エンティティがそのまま残っていることがある(&quot; &lt;a href= 等)
    for p in re.split(r"[/／,、\n・]+", _html.unescape(s or "")):
        p = re.sub(r"\s+", " ", p).strip(" \t:：-—–")
        if not p or len(p) > 30:
            continue
        if _NOT_A_NAME.search(p):
            continue
        if not re.search(r"[0-9A-Za-zぁ-んァ-ヶ一-龥]", p):
            continue
        if re.fullmatch(r"[\d\s:：時分番〜~\-]+", p):  # 時刻・整理番号だけの断片
            continue
        if p.endswith("。") or "、" in p:  # 説明文であって名前ではない
            continue
        if re.search(r"\d{4}年|\d{1,2}月\d{1,2}日", p):  # 日付の断片
            continue
        out.append(p)
    return out


def _livepocket_performers(html: str) -> List[str]:
    """出演者は /event/search?performer=... へのリンクとして並ぶので、そこから取る。
    販売元は /p/... なので自然に除外される。"""
    import urllib.parse
    out, seen = [], set()
    for raw in re.findall(r'href="/event/search\?performer=([^"]+)"', html):
        name = urllib.parse.unquote_plus(raw).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _page_lines(html: str):
    """ページ本文を行に戻す。JSON-LD に無い情報(販売受付期間・券種・先着)を読むため。"""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html or "")
    t = re.sub(r"<[^>]+>", "\n", t)
    t = _html.unescape(t)
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


# 「2026年10月2日(金) 21:00」
_JP_DT_LINE = re.compile(
    r"(20\d\d)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\d]{0,6}(\d{1,2})\s*[:：]\s*(\d{2})")


def _livepocket_sale(lines):
    """LivePocket の「販売受付期間」から発売開始日時を取る。

    LivePocket の JSON-LD には validFrom が無く(offers は価格だけ)、
    発売開始はページ本文にしか書かれていない。
    """
    for i, ln in enumerate(lines):
        if "販売受付期間" not in ln and "受付期間" not in ln:
            continue
        # 見出しの次の数行に「2026年10月2日(金) 21:00」「〜2026年10月17日(土) 15:00」と続く
        for nxt in lines[i + 1:i + 5]:
            if nxt.startswith(("〜", "~")):
                continue  # 終了日時なので使わない
            m = _JP_DT_LINE.search(nxt)
            if m:
                y, mo, d, h, mi = (int(x) for x in m.groups())
                return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:00"
    return None


# 券種名の下に出る販売状況のラベル。券種名と間違えて拾わないように除く
# (「予定販売数終了」「売切間近」が券種名として保存されていた)
_LP_BADGE = re.compile(
    r"^(?:販売(?:中|前|開始前|終了|休止中)|予定販売数終了|売(?:切|り切れ)(?:間近)?|"
    r"完売(?:間近)?|受付(?:中|前|終了)|残り(?:わずか|少)|キャンセル待ち|"
    r"取扱(?:終了|開始前)|[¥￥（(].*)$")


def _livepocket_ticket_types(lines):
    """「前方チケット / 販売中 / ¥4,000」の並びから券種と価格を拾う。"""
    out = []
    for i, ln in enumerate(lines):
        if not re.match(r"^[¥￥]\s*[\d,]+$", ln):
            continue
        price = ln.replace("￥", "¥").replace("¥", "").strip()
        # 価格の上に「販売中」などの状態、その上に券種名が来る
        for back in range(1, 4):
            if i - back < 0:
                break
            cand = lines[i - back]
            if _LP_BADGE.match(cand):
                continue
            if 1 <= len(cand) <= 40:
                out.append({"name": cand, "price": f"{price}円"})
            break
    # 同じ券種が複数回出ることがあるので重複を除く
    seen, uniq = set(), []
    for t in out:
        k = (t["name"], t["price"])
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


# 抽選・先行抽選をうたう文言。これが無ければ先着として扱う
_LOTTERY_RE = re.compile(r"抽選|先行受付|先行販売|プレオーダー|申込多数")


def _first_come(lines, description: str):
    """先着かどうか。LivePocket は「先着」と明記される。

    TiGET / LivePocket はどちらも申し込み順に売る仕組みで、抽選は主催が
    別途案内する形。明記が無いときは先着として扱い、抽選をうたう文言が
    あるときだけ False にする。
    """
    text = "\n".join(lines[:400]) + "\n" + (description or "")
    if "先着" in text:
        return True
    return not _LOTTERY_RE.search(text)


def fetch_event(url: str) -> Optional[dict]:
    """LivePocket / TiGET のイベントページを解析して共通形式の dict を返す。"""
    site = site_of(url)
    if not site:
        return None
    html = _get(url)
    if not html:
        return None
    block = _ldjson_block(html)
    if not block:
        print(f"    JSON-LD が見つかりません: {url}")
        return None

    name = _json_string(block, "name")
    start = _json_string(block, "startDate")
    venue = _nested_name(block, "location")
    description = _json_string(block, "description") or ""
    price = None
    mp = re.search(r'"price"\s*:\s*"?([0-9,]+)"?', block)
    if mp:
        price = mp.group(1).replace(",", "")
    sale = _sale_datetime(_json_string(block, "validFrom") or "")
    lines = _page_lines(html)
    ticket_types = (
        [{"name": "チケット", "price": f"{int(price):,}円"}] if price and price.isdigit() else []
    )
    if site == "livepocket":
        # JSON-LD に無い発売開始・券種は本文から取る
        sale = sale or _livepocket_sale(lines)
        ticket_types = _livepocket_ticket_types(lines) or ticket_types

    if site == "livepocket":
        performers = _livepocket_performers(html) or _split_performers(_nested_name(block, "performer") or "")
        act_text, act_labeled = " / ".join(performers), bool(performers)
    else:  # tiget: performer は壊れているので description の出演欄を使う
        act_text, act_labeled = _act_text(description)
        performers = _split_performers(act_text)

    return {
        "source": site,
        "url": url,
        "event_name": name,
        "event_datetime": _jp_datetime(start or ""),
        "venue": venue,
        "sale_datetime": sale,
        "is_first_come": _first_come(lines, description),
        "ticket_types": ticket_types,
        "performers": performers,
        "act_text": act_text,
        "act_labeled": act_labeled,
    }

# LivePocket の「ファン・アイドル」カテゴリを公開日が新しい順に並べた一覧。
# X への投稿を待たずに、公開された時点で発売を拾うために使う。
LIVEPOCKET_IDOL_LIST = (
    "https://livepocket.jp/event/search"
    "?l_cat=%E3%83%95%E3%82%A1%E3%83%B3%E3%83%BB%E3%82%A2%E3%82%A4%E3%83%89%E3%83%AB&sort=3"
)

_CARD_SPLIT = re.compile(r'<li class="event-card-list__item">')
_CARD_HREF = re.compile(r'href="(/e/[^"]+)"')
_CARD_TAG = re.compile(r'class="[^"]*event-card__tag[^"]*">([^<]+)<')
_CARD_TITLE = re.compile(r'class="event-card__title">(.*?)</h3>', re.S)
# 「<span class="event-card__place">会場</span> 新宿MARZ（東京都）」の後ろ側
_CARD_FIELD = {
    "date": re.compile(r'class="event-card__date">[^<]*</span>(.*?)</p>', re.S),
    "venue": re.compile(r'class="event-card__place">[^<]*</span>(.*?)</p>', re.S),
    "cast": re.compile(r'class="event-card__cast">[^<]*</span>(.*?)</p>', re.S),
}


def _card_text(block: str, pat) -> str:
    m = pat.search(block)
    if not m:
        return ""
    return _html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def _get_listing(url: str, timeout: int = 25):
    """一覧ページを取る。実行環境によって中身が空で返ることがあるので、
    何が返ってきたかを必ず残す(本文が空だと例外にならず、原因が分からない)。"""
    _pace()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
        "Referer": "https://livepocket.jp/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    })
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        html = data.decode("utf-8", "replace")
        if not html.strip():
            print(f"    一覧の本文が空です(HTTP {r.status} / {len(data)}バイト): {url[:70]}")
        if bot_wall(html):
            return _BROWSER.get(url)
        return html
    except Exception as e:
        print(f"    一覧の取得に失敗: {type(e).__name__} {e} / {url[:70]}")
        return None


def livepocket_listing(pages: int = 3):
    """アイドルカテゴリの新着一覧を読み、カードの内容をそのまま返す。

    一覧に「販売前」の表示と出演者が載っているので、ここで絞ってから
    イベントページを取りに行けば、無駄な取得をほとんどせずに済む。
    返り値: [{url, status, name, date, venue, performers[]}]
    """
    out = []
    for page in range(1, max(1, pages) + 1):
        url = LIVEPOCKET_IDOL_LIST + (f"&page={page}" if page > 1 else "")
        html = _get_listing(url)
        if not html:
            break
        blocks = _CARD_SPLIT.split(html)[1:]
        if not blocks:
            # 実行環境によっては別のページ(同意画面など)が返ることがあるので、
            # 何が返ってきたか分かるようにしておく
            title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
            head = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[:400])).strip()
            print(f"    LivePocket 一覧にカードがありません({len(html)}字"
                  f" / title={_html.unescape(title.group(1)).strip()[:40] if title else 'なし'})"
                  f" 冒頭: {head[:120]}")
            break
        for b in blocks:
            m = _CARD_HREF.search(b)
            if not m:
                continue
            cast = _card_text(b, _CARD_FIELD["cast"])
            out.append({
                "url": "https://livepocket.jp" + m.group(1),
                "status": _card_text(b, _CARD_TAG),
                "name": _card_text(b, _CARD_TITLE),
                "date": _card_text(b, _CARD_FIELD["date"]),
                "venue": _card_text(b, _CARD_FIELD["venue"]),
                "performers": _split_performers(cast),
            })
    return out

# TiGET のアイドルカテゴリ一覧(categories=29)
TIGET_IDOL_LIST = "https://tiget.net/events?categories=29"


def organizer_of(html: str):
    """イベントページから主催者ページ(/p/<slug>)を取り出す。

    LivePocket の検索・一覧は AWS WAF の内側だが、主催者ページは通る。
    同じ主催者は繰り返し公演を打つので、ここを辿れば検索を使わずに
    新しい公演を見つけられる。
    """
    m = re.search(r'href="(/p/[A-Za-z0-9_-]+)"', html or "")
    return "https://livepocket.jp" + m.group(1) if m else None


def livepocket_organizer_events(url: str):
    """主催者ページに並ぶイベントURLを返す。"""
    html = _get_listing(url)
    if not html:
        return []
    return sorted({"https://livepocket.jp" + p
                   for p in re.findall(r"/e/[A-Za-z0-9_-]+", html)})


def listing_check(urls=None):
    """一覧ページに実行環境からアクセスできるかを調べて表示する。

    LivePocket の一覧は AWS WAF のボット対策で、データセンターからは
    認証ページが返る。他サイトも同じ扱いかどうかは実際に取ってみないと
    分からないので、手を入れる前にこれで確かめる。
    """
    for url in urls or [TIGET_IDOL_LIST, LIVEPOCKET_IDOL_LIST]:
        html = _get_listing(url)
        if html is None:
            print(f"  取得できません: {url[:70]}")
            continue
        title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = _html.unescape(title.group(1)).strip()[:48] if title else "なし"
        waf = "awsWafCookieDomainList" in html or "gokuProps" in html
        events = len(set(re.findall(r"/events/\d+|/e/[A-Za-z0-9_-]+", html)))
        head = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[:300])).strip()
        print(f"  {url[:60]}")
        print(f"    {len(html)}字 / title={title} / ボット対策={'あり' if waf else 'なし'}"
              f" / イベントURL {events}件")
        if waf or events == 0:
            print(f"    冒頭: {head[:110]}")
