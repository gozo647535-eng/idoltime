"""TicketDive(Ticketdive / t-dv.com)イベントページの構造化パーサ。

TicketDive は Next.js 製で、ページHTMLの <script id="__NEXT_DATA__"> に superjson
形式でイベント情報が丸ごと入っている。ここから会場・公演日時・発売日時・受付方式
(先着/抽選)・券種と料金を確実に取り出せるため、Gemini不要でチケット情報を高精度化
できる。auto_events の ticket_url が TicketDive の場合に呼んで補完する。
"""
import datetime
import gzip
import json
import re
import urllib.request
from typing import List, Optional

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
JST = datetime.timezone(datetime.timedelta(hours=9))


def is_ticketdive_url(url: Optional[str]) -> bool:
    if not url:
        return False
    return ("ticketdive.com/event/" in url) or ("t-dv.com/" in url)


def _get(url: str, timeout: int = 20) -> Optional[str]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept-Language": "ja,en;q=0.8"}
        )
        r = urllib.request.urlopen(req, timeout=timeout)  # リダイレクト(t-dv→ticketdive)は自動追従
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")
    except Exception as e:
        print(f"    TicketDive 取得失敗 {url}: {e}")
        return None


def _iso_to_jst(iso: Optional[str], with_time: bool = True) -> Optional[str]:
    """'2026-07-16T12:00:00.000Z'(UTC)→ JSTの 'YYYY-MM-DD HH:MM[:SS]'。"""
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s).astimezone(JST)
        return dt.strftime("%Y-%m-%d %H:%M:%S") if with_time else dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _pick_ticket_info(ticket_info_list: List[dict]) -> Optional[dict]:
    """複数の受付(先行/一般など)から、今後の受付を優先して1件選ぶ。
    未来の startApply が最も近いもの、無ければ先頭。"""
    if not ticket_info_list:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)

    def start_dt(ti):
        s = ti.get("startApply") or ti.get("release")
        try:
            return datetime.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        except Exception:
            return None

    upcoming = [(start_dt(ti), ti) for ti in ticket_info_list]
    future = [(d, ti) for d, ti in upcoming if d and d >= now]
    if future:
        return min(future, key=lambda x: x[0])[1]
    return ticket_info_list[0]


def fetch_ticketdive(url: str) -> Optional[dict]:
    """TicketDive イベントページを解析して構造化情報を返す。失敗時 None。
    返り値: event_name, venue, event_datetime, sale_datetime, sale_end,
            is_first_come, ticket_types[{name,price}], artists[str]。"""
    if not is_ticketdive_url(url):
        return None
    html = _get(url)
    if not html:
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        nxt = json.loads(m.group(1))
        ed = nxt["props"]["pageProps"]["__superjsonProps"]["json"]["eventDetail"]
    except Exception as e:
        print(f"    TicketDive 解析失敗 {url}: {e}")
        return None

    event = ed.get("event") or {}
    stages = ed.get("stages") or []
    stage = stages[0] if stages else {}
    venue_obj = stage.get("venue") or {}
    venue = venue_obj.get("name") or stage.get("stageName") or None
    if venue and venue_obj.get("address"):
        venue = f"{venue}({venue_obj['address']})"

    ti = _pick_ticket_info(ed.get("ticketInfoList") or [])
    sale_dt = _iso_to_jst(ti.get("startApply") or ti.get("release")) if ti else None
    sale_end = _iso_to_jst(ti.get("endApply")) if ti else None
    is_first_come = (ti.get("receptionType") == "first") if ti else False
    ticket_types = []
    for tt in (ti.get("ticketTypes") if ti else []) or []:
        name = tt.get("name")
        price = tt.get("price")
        if name is not None:
            price_str = f"{price:,}円" if isinstance(price, (int, float)) else (str(price) if price else "")
            ticket_types.append({"name": name, "price": price_str})

    return {
        "event_name": event.get("name"),
        "venue": venue,
        "event_datetime": _iso_to_jst(stage.get("startStage"), with_time=True),
        "sale_datetime": sale_dt,
        "sale_end": sale_end,
        "is_first_come": is_first_come,
        "ticket_types": ticket_types,
        "artists": [a.get("name") for a in (stage.get("artists") or []) if a.get("name")],
    }
