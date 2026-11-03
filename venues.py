"""会場の公式スケジュールから公演を読む(レジストリの読み込み・取得・抽出)。

読むのはこのモジュール、判定と出力は worker の run_venues、という分担
(release.py と同じ考え方)。ここは DB にも LLM にも触れない。

Phase 1 の抽出方式は method=selector と method=jsonld。

venues.json の selector の項目:

    item          1公演の要素(必須)
    item_next     1公演が兄弟要素のペアに分かれるサイト用。item の直後の要素がこのセレクタに
                  合うときだけ、同じ公演として一緒に読む(直後でなければペアにしない)
    section       ページ内の範囲。1ページに複数会場が同居するサイト用の拡張
    date / title / start / open / artist / link
                  項目内の CSS セレクタ。文字列か、優先順に並べた配列
    date_attr     日付を date の要素のこの属性から読む(8桁の YYYYMMDD を探す)。
                  読めなければ date の要素の文字から読む
    artist_strip  artist の文字から取り除く正規表現(ラベルと値が同じ要素にある作り用)
    blank_titles  これだけが書かれていたら公演名なしと扱う語(「Private」など)
    id_pattern    詳細URLから安定IDを取り出す正規表現(group 1)

venues.json の jsonld の項目(ページ内の schema.org Event を読む。公演日は startDate):

    id_pattern    Event の url から安定IDを取り出す正規表現(group 1)
    category      同じページの要素で Event を絞る。item は分類の要素、id_class はその class から
                  ID を取り出す正規表現(group 1)、allow_class は対象とする分類の class
"""
import copy
import json
import os
import re
import time
import unicodedata
import datetime
import urllib.parse
from typing import Dict, List, Optional, Tuple

import soupsieve
from bs4 import BeautifulSoup

import sites

REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venues.json")

STATUSES = {"off", "shadow", "live", "none"}
ACTIVE_STATUSES = {"shadow", "live"}
METHODS = {"jsonld", "ical", "selector", "llm", "none"}
IMPLEMENTED_METHODS = {"selector", "jsonld"}

DEFAULT_MONTHS_AHEAD = 2
# 相手サイトへの配慮(キャッシュに当たらなかった取得の間隔)
REQUEST_INTERVAL = 1.0
# 先頭で走るので、遅いサイトが後続の収集を圧迫しないよう全体の時間を区切る
DEFAULT_TIME_BUDGET_SEC = 600
# この数を超えて置換文字があれば、文字コードの判定を誤っている疑い
_MOJIBAKE_MIN = 20
ITEM_TEXT_MAX = 1000

_ID_RE = re.compile(r"^[a-z0-9-]+$")
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)
_CSS_FIELDS = ("date", "title", "start", "open", "artist", "link")


# --- レジストリ ---

def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _css_errors(spec, label: str) -> List[str]:
    if spec is None:
        return []
    if isinstance(spec, str):
        specs = [spec]
    elif _is_str_list(spec):
        specs = spec
    else:
        return [f"{label} は文字列か配列にしてください"]
    errors = []
    for s in specs:
        try:
            soupsieve.compile(s)
        except Exception as e:
            errors.append(f"{label} のセレクタが不正: {s} ({e})")
    return errors


def _pattern_errors(pattern, label: str, needs_group: bool = True) -> List[str]:
    if pattern is None:
        return []
    try:
        if needs_group and re.compile(pattern).groups < 1:
            return [f"{label} にはグループが必要です"]
        re.compile(pattern)
    except (re.error, TypeError) as e:
        return [f"{label} が不正: {e}"]
    return []


def _selector_errors(sel) -> List[str]:
    if not isinstance(sel, dict):
        return ["method=selector には selector が必要です"]
    errors = []
    if not isinstance(sel.get("item"), str) or not sel["item"].strip():
        errors.append("selector.item がありません")
    else:
        errors += _css_errors(sel["item"], "selector.item")
    errors += _css_errors(sel.get("section"), "selector.section")
    errors += _css_errors(sel.get("item_next"), "selector.item_next")
    for f in _CSS_FIELDS:
        errors += _css_errors(sel.get(f), f"selector.{f}")
    if sel.get("date_attr") is not None and not isinstance(sel["date_attr"], str):
        errors.append("selector.date_attr は文字列にしてください")
    if sel.get("blank_titles") is not None and not _is_str_list(sel["blank_titles"]):
        errors.append("selector.blank_titles は文字列の配列にしてください")
    errors += _pattern_errors(sel.get("id_pattern"), "selector.id_pattern")
    errors += _pattern_errors(sel.get("artist_strip"), "selector.artist_strip", needs_group=False)
    return errors


def _jsonld_errors(conf) -> List[str]:
    if conf is None:
        return []
    if not isinstance(conf, dict):
        return ["jsonld はオブジェクトにしてください"]
    errors = _pattern_errors(conf.get("id_pattern"), "jsonld.id_pattern")
    cat = conf.get("category")
    if cat is None:
        return errors
    if not isinstance(cat, dict):
        return errors + ["jsonld.category はオブジェクトにしてください"]
    # 分類で絞るには Event 側の ID も要る
    if not conf.get("id_pattern"):
        errors.append("jsonld.category には jsonld.id_pattern が必要です")
    if not isinstance(cat.get("item"), str) or not cat["item"].strip():
        errors.append("jsonld.category.item がありません")
    else:
        errors += _css_errors(cat["item"], "jsonld.category.item")
    if not cat.get("id_class"):
        errors.append("jsonld.category.id_class がありません")
    errors += _pattern_errors(cat.get("id_class"), "jsonld.category.id_class")
    if not isinstance(cat.get("allow_class"), str) or not cat["allow_class"].strip():
        errors.append("jsonld.category.allow_class がありません")
    return errors


def validate_venue(v) -> List[str]:
    """会場1件の問題点の一覧(空なら正常)。"""
    if not isinstance(v, dict):
        return ["会場がオブジェクトではありません"]
    errors = []
    if not isinstance(v.get("id"), str) or not _ID_RE.match(v["id"]):
        errors.append("id は英小文字・数字・ハイフンのみです")
    for f in ("name", "prefecture", "city"):
        if not isinstance(v.get(f), str) or not v[f].strip():
            errors.append(f"{f} がありません")
    if not _is_str_list(v.get("aliases")):
        errors.append("aliases は文字列の配列にしてください")
    status, method = v.get("status"), v.get("method")
    if status not in STATUSES:
        errors.append(f"status が不正: {status}")
    if method not in METHODS and not (isinstance(method, str) and method.startswith("parser:")):
        errors.append(f"method が不正: {method}")
    if not isinstance(v.get("official_url"), str):
        errors.append("official_url がありません")
    if not _is_str_list(v.get("schedule_urls")):
        errors.append("schedule_urls は文字列の配列にしてください")
    months = v.get("months_ahead")
    if months is not None and (not isinstance(months, int) or isinstance(months, bool) or months < 0):
        errors.append("months_ahead は0以上の整数にしてください")
    minimum = v.get("expected_min_entries")
    if minimum is not None and (not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0):
        errors.append("expected_min_entries は0以上の整数にしてください")
    flt = v.get("filter")
    if flt is not None:
        if not isinstance(flt, dict):
            errors.append("filter はオブジェクトにしてください")
        else:
            for f in ("allow_titles", "deny_titles"):
                if flt.get(f) is not None and not _is_str_list(flt[f]):
                    errors.append(f"filter.{f} は文字列の配列にしてください")
    if status in ACTIVE_STATUSES:
        # off / none は URL が未確認でも登録できる。実際に読む会場だけ厳しく見る
        if not _HTTP_RE.match(str(v.get("official_url") or "")):
            errors.append("official_url は http(s) で指定してください")
        urls = v.get("schedule_urls")
        if not urls or (_is_str_list(urls) and not all(_HTTP_RE.match(u) for u in urls)):
            errors.append("schedule_urls に http(s) のURLが必要です")
        if method == "none":
            errors.append(f"status={status} に method=none は使えません")
    if method == "selector":
        errors += _selector_errors(v.get("selector"))
    if method == "jsonld":
        errors += _jsonld_errors(v.get("jsonld"))
    return errors


def load_registry(path: str = REGISTRY_PATH) -> List[dict]:
    """正常な会場だけを返す。不正な会場は警告して飛ばし、全体は止めない。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"⚠ 会場レジストリを読めません({path}): {e}")
        return []
    if not isinstance(data, list):
        print(f"⚠ 会場レジストリの形式が不正です(配列ではない): {path}")
        return []
    out, seen = [], set()
    for i, v in enumerate(data):
        errors = validate_venue(v)
        vid = v.get("id") if isinstance(v, dict) else None
        if not errors and vid in seen:
            errors = [f"id が重複しています: {vid}"]
        if errors:
            print(f"⚠ 会場 {vid or f'#{i}'} を飛ばします: {' / '.join(errors)}")
            continue
        seen.add(vid)
        out.append(v)
    return out


def select_targets(registry: List[dict], ids: Optional[List[str]] = None) -> List[dict]:
    """巡回する会場。ids を渡すとそれだけに絞る(off / none は指定しても読まない)。"""
    wanted = {i.strip() for i in ids or [] if i.strip()}
    known = {v["id"] for v in registry}
    for missing in sorted(wanted - known):
        print(f"⚠ VENUE_IDS の会場がレジストリにありません: {missing}")
    out = []
    for v in registry:
        if wanted and v["id"] not in wanted:
            continue
        if v["status"] not in ACTIVE_STATUSES:
            if wanted:
                print(f"会場 {v['id']} は status={v['status']} なので読みません")
            continue
        if v["method"] not in IMPLEMENTED_METHODS:
            print(f"⚠ 会場 {v['id']} の method={v['method']} は未対応なので飛ばします")
            continue
        out.append(v)
    return out


def expand_schedule_urls(venue: dict, today: str) -> List[Tuple[str, Optional[int], Optional[int]]]:
    """schedule_urls を (URL, ページの年, ページの月) に展開する。月別でなければ年月は None。"""
    base = datetime.date.fromisoformat(today)
    months = venue.get("months_ahead")
    months = DEFAULT_MONTHS_AHEAD if months is None else months
    out = []
    for tmpl in venue.get("schedule_urls") or []:
        if "{yyyy}" not in tmpl and "{mm}" not in tmpl:
            out.append((tmpl, None, None))
            continue
        for i in range(months + 1):
            total = base.year * 12 + (base.month - 1) + i
            y, m = total // 12, total % 12 + 1
            out.append((tmpl.replace("{yyyy}", f"{y:04d}").replace("{mm}", f"{m:02d}"), y, m))
    return out


# --- 日付・時刻 ---

def _nfkc(s: Optional[str]) -> str:
    return unicodedata.normalize("NFKC", s or "")


_Y_DATE = re.compile(r"(?<!\d)(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})(?!\d)")
_MD_DATE = re.compile(r"(?<![\d/.\-])(\d{1,2})\s*(?:月\s*(\d{1,2})|[/.]\s*(\d{1,2}))(?!\d)")
_D_ONLY = re.compile(r"^\s*(\d{1,2})(?![\d:時/.])")


def _make_date(y: int, m: int, d: int) -> Optional[datetime.date]:
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def _year_from_page(month: int, page_year: int, page_month: int) -> int:
    """月別ページの年月から年を決める。12月のページの1月は翌年、1月のページの12月は前年。"""
    if month - page_month > 6:
        return page_year - 1
    if page_month - month > 6:
        return page_year + 1
    return page_year


def _nearest_date(month: int, day: int, today: datetime.date) -> Optional[datetime.date]:
    """年の手がかりが無いときは、今日に最も近い年にする。

    「過去60日より前なら翌年」の規則(normalize_event_datetime)だと、一覧に残った
    2か月以上前の公演が来年の公演に化けて、過去の判定をすり抜ける。
    """
    cands = [_make_date(y, month, day) for y in (today.year - 1, today.year, today.year + 1)]
    cands = [c for c in cands if c]
    return min(cands, key=lambda c: abs((c - today).days)) if cands else None


def parse_date(text: Optional[str], page_year: Optional[int] = None,
               page_month: Optional[int] = None, today: Optional[str] = None,
               allow_day_only: bool = False) -> Optional[str]:
    """文字列中の公演日を 'YYYY-MM-DD' で返す。年なしは月別ページの年月を優先して補う。

    allow_day_only は日付だけが入る要素を指定して読むときのみ。本文から日の数字だけを
    拾うと誤読するので、月別ページの年月が分かっている場合に限る。
    """
    t = _nfkc(text)
    m = _Y_DATE.search(t)
    if m:
        got = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return got.isoformat() if got else None
    base = datetime.date.fromisoformat(today) if today else datetime.date.today()
    m = _MD_DATE.search(t)
    if m:
        month, day = int(m.group(1)), int(m.group(2) or m.group(3))
        if page_year and page_month:
            got = _make_date(_year_from_page(month, page_year, page_month), month, day)
        else:
            got = _nearest_date(month, day, base)
        return got.isoformat() if got else None
    if allow_day_only and page_year and page_month:
        m = _D_ONLY.search(t)
        if m:
            got = _make_date(page_year, page_month, int(m.group(1)))
            return got.isoformat() if got else None
    return None


# 「17;30」は EDWARD の実ページにある誤記
_T = r"\d{1,2}(?:[:;]\d{2}|時(?:\d{1,2}分?)?)"
_OPEN_WORD = r"(?:OPEN|開場)"
_START_WORD = r"(?:START|開演|スタート)"
_TIME_PAIR = re.compile(
    rf"{_OPEN_WORD}\s*[/・|&]\s*{_START_WORD}\s*:?\s*({_T})\s*[/・|~〜\-]\s*({_T})", re.IGNORECASE)
_TIME_START = re.compile(rf"{_START_WORD}\s*[:\-]?\s*({_T})", re.IGNORECASE)
_TIME_OPEN = re.compile(rf"{_OPEN_WORD}\s*[:\-]?\s*({_T})", re.IGNORECASE)
_TIME_ANY = re.compile(_T)
_CIRCLED_NUMBER = re.compile("[①-⑳]")


def _time_text(s: Optional[str]) -> str:
    """時刻を読む前の正規化。

    2部制の「①12:45②16:15」は NFKC だと「112:45216:15」に潰れて読めないので、
    丸数字は先に区切りへ置き換える。
    """
    return _nfkc(_CIRCLED_NUMBER.sub(" ", s or ""))


def _hhmm(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.match(r"(\d{1,2})(?:[:;](\d{2})|時(\d{1,2})?)", s)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or m.group(3) or 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_times(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(開場, 開演) を返す。ラベルの無い時刻は開演として扱わない。2部制は最初の回の時刻。"""
    t = _time_text(text)
    m = _TIME_PAIR.search(t)
    if m:
        return _hhmm(m.group(1)), _hhmm(m.group(2))
    start = _TIME_START.search(t)
    open_ = _TIME_OPEN.search(t)
    return _hhmm(open_.group(1)) if open_ else None, _hhmm(start.group(1)) if start else None


# --- 取得 ---

class Fetcher:
    """1回の巡回の間、同じURLを取り直さない取得口。

    同じページに会場が同居するサイト(EDWARD)でも取得と解析は1回で済む。
    失敗も覚えておき、同居する会場の分で取り直さない。
    """

    def __init__(self, budget_sec: Optional[float] = None):
        budget = DEFAULT_TIME_BUDGET_SEC if budget_sec is None else budget_sec
        self._deadline = time.monotonic() + budget
        self._cache: Dict[str, Optional[BeautifulSoup]] = {}
        self._fetched_any = False
        self._expired_noted = False
        self.fetch_count = 0

    def soup(self, url: str, render: bool = False) -> Optional[BeautifulSoup]:
        if url in self._cache:
            return self._cache[url]
        if time.monotonic() > self._deadline:
            if not self._expired_noted:
                print("⚠ 会場収集の時間上限に達したので、以降の取得を打ち切ります")
                self._expired_noted = True
            return None
        if self._fetched_any and REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL)
        self._fetched_any = True
        self.fetch_count += 1
        html = sites._render_page(url) if render else sites._get(url)
        if html and html.count("�") >= _MOJIBAKE_MIN:
            print(f"⚠ 文字化けの疑い(文字コードの判定を確認): {url}")
        parsed = BeautifulSoup(html, "html.parser") if html else None
        self._cache[url] = parsed
        return parsed


# --- 抽出 ---

def _specs(spec) -> List[str]:
    if not spec:
        return []
    return [spec] if isinstance(spec, str) else list(spec)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _block_text(node) -> str:
    return node.get_text("\n", strip=True)


def _pick_node(node, spec):
    """優先順に試して、文字が入っている最初の要素を返す。"""
    for css in _specs(spec):
        for found in node.select(css):
            if found.get_text(strip=True):
                return found
    return None


def _pick_text(node, spec) -> Optional[str]:
    found = _pick_node(node, spec)
    return _clean(found.get_text(" ", strip=True)) if found else None


def _pick_link(node, spec, base_url: str) -> Optional[str]:
    anchors = []
    for css in _specs(spec):
        anchors.extend(node.select(css))
    if not spec:
        if node.name == "a":
            anchors.append(node)
        anchors.extend(node.select("a[href]"))
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        return urllib.parse.urljoin(base_url, href)
    return None


def _is_blank_title(title: Optional[str], blank_titles) -> bool:
    norm = _clean(_nfkc(title)).casefold()
    return bool(norm) and any(norm == _clean(_nfkc(b)).casefold() for b in blank_titles or [])


def _times_from(node, spec_start, spec_open, item_text: str) -> Tuple[Optional[str], Optional[str]]:
    open_, start = parse_times(item_text)
    start_text = _pick_text(node, spec_start)
    if start_text:
        o2, s2 = parse_times(start_text)
        if s2:
            start = s2
        elif not o2:
            # 開演だけを指す要素。「OPEN 17:30」しか無い要素を開演と読まない
            m = _TIME_ANY.search(_time_text(start_text))
            start = _hhmm(m.group(0)) if m else start
    open_text = _pick_text(node, spec_open)
    if open_text:
        o2, s2 = parse_times(open_text)
        if o2:
            open_ = o2
        elif not s2:
            m = _TIME_ANY.search(_time_text(open_text))
            open_ = _hhmm(m.group(0)) if m else open_
    return open_, start


def _with_next(node, css: str):
    """item と直後の兄弟要素を1公演として読むための入れ物を返す。ページの木は変えない。"""
    following = node.find_next_sibling()
    if following is None or not soupsieve.match(css, following):
        return node
    pair = BeautifulSoup("", "html.parser").new_tag("div")
    pair.append(copy.copy(node))
    pair.append(copy.copy(following))
    return pair


_YMD_IN_ATTR = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")


def _date_from(node, sel: dict, item_text: str, page_year: Optional[int],
               page_month: Optional[int], today: Optional[str]) -> Optional[str]:
    if not sel.get("date"):
        return parse_date(item_text, page_year, page_month, today)
    found = _pick_node(node, sel["date"])
    if found is None:
        return None
    if sel.get("date_attr"):
        m = _YMD_IN_ATTR.search(str(found.get(sel["date_attr"]) or ""))
        got = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
        if got:
            return got.isoformat()
    return parse_date(_clean(found.get_text(" ", strip=True)), page_year, page_month, today,
                      allow_day_only=True)


def _artist_from(node, sel: dict) -> Optional[str]:
    text = _pick_text(node, sel.get("artist"))
    if text and sel.get("artist_strip"):
        text = _clean(re.sub(sel["artist_strip"], "", text, flags=re.IGNORECASE))
    return text or None


def _ext_id(link: Optional[str], pattern: Optional[str]) -> Optional[str]:
    if not link or not pattern:
        return None
    m = re.search(pattern, link)
    return m.group(1) if m else None


def extract_items(soup, venue: dict, base_url: str, page_year: Optional[int] = None,
                  page_month: Optional[int] = None, today: Optional[str] = None) -> List[dict]:
    """一覧ページから公演の候補を取り出す。判定はせず、全ジャンルをそのまま返す。"""
    sel = venue["selector"]
    root = soup
    if sel.get("section"):
        root = soup.select_one(sel["section"])
        if root is None:
            print(f"⚠ {venue['id']}: セクション {sel['section']} がページにありません")
            return []
    out = []
    for item in root.select(sel["item"]):
        node = _with_next(item, sel["item_next"]) if sel.get("item_next") else item
        item_text = _block_text(node)
        title = _pick_text(node, sel.get("title"))
        blank = _is_blank_title(title, sel.get("blank_titles"))
        date = _date_from(node, sel, item_text, page_year, page_month, today)
        open_, start = _times_from(node, sel.get("start"), sel.get("open"), item_text)
        artist = _artist_from(node, sel)
        link = _pick_link(node, sel.get("link"), base_url)
        ext_id = _ext_id(link, sel.get("id_pattern"))
        out.append({
            "venue_id": venue["id"],
            "title": None if blank else title,
            "blank_title": blank,
            "date": date,
            "start": start,
            "open": open_,
            "artist": None if blank else artist,
            "body": None,
            "link": link,
            "ext_id": ext_id,
            "source_url": base_url,
            "item_text": item_text[:ITEM_TEXT_MAX],
        })
    return out


def _jsonld_events(soup):
    """ページ内の schema.org Event を順に返す。壊れた JSON は飛ばす。"""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for obj in sites._iter_jsonld_objects(data):
            if isinstance(obj, dict) and sites._jsonld_type_is_event(obj):
                yield obj


def _allowed_category_ids(soup, venue: dict, cat: dict) -> set:
    """対象の分類に属する要素の ID を集める。"""
    nodes = soup.select(cat["item"])
    if not nodes:
        print(f"⚠ {venue['id']}: 分類の要素 {cat['item']} がページにありません")
    ids = set()
    for node in nodes:
        classes = node.get("class") or []
        if cat["allow_class"] not in classes:
            continue
        for c in classes:
            m = re.search(cat["id_class"], c)
            if m:
                ids.add(m.group(1))
    return ids


def extract_jsonld(soup, venue: dict, base_url: str) -> List[dict]:
    """JSON-LD の Event から公演の候補を取り出す。公演日は startDate を正とする。

    sites._parse_jsonld_events は実時計で過去を落とし、結果の SiteEvent も分類との照合や
    判定に使う項目を持たないので、ここでは下請けの関数だけを共有する。
    startDate の時刻は開演と限らないので使わない(T00:00:00 は時刻なしの意味)。
    """
    conf = venue.get("jsonld") or {}
    cat = conf.get("category")
    allowed = _allowed_category_ids(soup, venue, cat) if cat else None
    out = []
    for obj in _jsonld_events(soup):
        link = sites._ld_url(obj.get("url"))
        ext_id = _ext_id(link, conf.get("id_pattern"))
        if allowed is not None and ext_id not in allowed:
            continue  # 分類を確かめられないものは対象にしない
        title = _clean(sites._ld_text(obj.get("name")) or "") or None
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(obj.get("startDate") or ""))
        got = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
        out.append({
            "venue_id": venue["id"],
            "title": title,
            "blank_title": False,
            "date": got.isoformat() if got else None,
            "start": None,
            "open": None,
            "artist": None,
            "body": None,
            "link": link,
            "ext_id": ext_id,
            "source_url": base_url,
            "item_text": (title or "")[:ITEM_TEXT_MAX],
        })
    return out


def _dedupe(events: List[dict]) -> List[dict]:
    """同じ公演を指す項目を畳む。"""
    seen, out = set(), []
    for ev in events:
        key = (ev["title"], ev["date"], ev["start"], ev["link"])
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def collect_venue(venue: dict, fetcher: Fetcher, today: str) -> Tuple[List[dict], List[str]]:
    """会場1件を読み、(公演の候補, 警告) を返す。件数はアイドル判定の前。"""
    render = bool(venue.get("render"))
    events, warnings, pages_ok = [], [], 0
    for url, year, month in expand_schedule_urls(venue, today):
        soup = fetcher.soup(url, render=render)
        if soup is None:
            warnings.append(f"取得失敗: {url}")
            continue
        pages_ok += 1
        if venue["method"] == "jsonld":
            items = extract_jsonld(soup, venue, url)
        else:
            items = extract_items(soup, venue, url, year, month, today)
        print(f"  {url}: {len(items)}件")
        events.extend(items)
    events = _dedupe(events)
    for ev in events:
        # 公演名の欄が無い作りでは、出演者を公演名にする
        if not ev["title"] and ev["artist"] and not ev["blank_title"]:
            ev["title"] = ev["artist"]
    if pages_ok and not events:
        warnings.append("抽出0件(全ジャンル)。セレクタが実ページと合っていない疑い")
    minimum = venue.get("expected_min_entries")
    if pages_ok and events and minimum and len(events) < minimum:
        warnings.append(f"抽出 {len(events)} 件が想定の最低 {minimum} 件を下回っています")
    return events, warnings
