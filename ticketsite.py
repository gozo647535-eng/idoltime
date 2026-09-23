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


def _get(url: str, timeout: int = 20) -> Optional[str]:
    try:
        dt = time.time() - _last_call[0]
        if dt < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - dt)
        _last_call[0] = time.time()
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
    r"(?:[¥￥]|\d+\s*円|https?://|[@＠]\w|[※⚠]|[【】]|お問い?合わせ|受付|予約|決済|"
    r"キャンセル|変更|入場|整理番号|ドリンク|物販|特典|開場|開演|終演|OPEN|START|DOOR|ADV)",
    re.IGNORECASE)


def _split_performers(s: str) -> List[str]:
    """出演者欄を名前らしい単位に割る。自由文なので、明らかに名前でない行は捨てる。"""
    out = []
    for p in re.split(r"[/／,、\n・]+", s or ""):
        p = re.sub(r"\s+", " ", p).strip(" \t:：-—–")
        if not p or len(p) > 30:
            continue
        if _NOT_A_NAME.search(p):
            continue
        if not re.search(r"[0-9A-Za-zぁ-んァ-ヶ一-龥]", p):
            continue
        if re.fullmatch(r"[\d\s:：時分番〜~\-]+", p):  # 時刻・整理番号だけの断片
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
        "ticket_types": ([{"name": "チケット", "price": f"{int(price):,}円"}] if price and price.isdigit() else []),
        "performers": performers,
        "act_text": act_text,
        "act_labeled": act_labeled,
    }
