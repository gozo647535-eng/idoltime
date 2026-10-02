"""Google カレンダーの公開カレンダー(ICS)からイベントを取り込む。

公式サイトにカレンダーを埋め込んでいるグループが一定数あり(埋め込みURLの
src= / cid= からカレンダーIDが取れる)、公開設定なら
  https://calendar.google.com/calendar/ical/<id>/public/basic.ics
が誰でも取得できる。TimeTree と同じ整形済み dict を返すので、呼び出し側は
同じ行組み立てを使える。LLM は使わない。
"""
import base64
import datetime
import hashlib
import re
import urllib.parse
import urllib.request

import timetree  # イベント名の整形・メモからのチケットURL抽出を共用

JST = datetime.timezone(datetime.timedelta(hours=9))
EXTRACT_VERSION = "gc1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def extract_calendar_id(url: str):
    """埋め込み/追加用URLからカレンダーIDを取り出す。取れなければ None。

    embed?src=<id> はそのまま、r?cid=<base64> は復号する。
    render?action=TEMPLATE は「予定を追加」用でカレンダーではないので弾く。
    """
    if not url:
        return None
    u = url.replace("&amp;", "&").replace("&#038;", "&")
    if "action=TEMPLATE" in u:
        return None
    if "/ical/" in u:  # 既に ICS のURL
        m = re.search(r"/ical/([^/]+)/", u)
        return urllib.parse.unquote(m.group(1)) if m else None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    if q.get("src"):
        return urllib.parse.unquote(q["src"][0])
    if q.get("cid"):
        v = urllib.parse.unquote(q["cid"][0])
        try:
            d = base64.b64decode(v + "=" * (-len(v) % 4)).decode("utf-8")
            if "@" in d:
                return d
        except Exception:
            pass
        return v if "@" in v else None
    return None


def ics_url(cal_id: str) -> str:
    return (f"https://calendar.google.com/calendar/ical/"
            f"{urllib.parse.quote(cal_id, safe='')}/public/basic.ics")


def _get(url: str, timeout: int = 25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"    Googleカレンダー取得失敗 {url[:70]}: {e}")
        return None


def _unfold(text: str):
    """ICS の折り返し(次行が空白始まり)を畳んで1行に戻す。"""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\;", ";").replace("\\\\", "\\"))


_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?")


def _parse_dt(value: str, params: str):
    """DTSTART の値を (date, 'HH:MM' or None) にする。失敗時 (None, None)。"""
    m = _DT.match(value.strip())
    if not m:
        return None, None
    y, mo, d, hh, mi = m.groups()
    try:
        day = datetime.date(int(y), int(mo), int(d))
    except ValueError:
        return None, None
    if hh is None:
        return day, None
    # UTC 表記(末尾Z)なら JST に直す。TZID 付きはそのまま現地時刻として扱う。
    if value.strip().endswith("Z"):
        dt = datetime.datetime(int(y), int(mo), int(d), int(hh), int(mi),
                               tzinfo=datetime.timezone.utc).astimezone(JST)
        return dt.date(), dt.strftime("%H:%M")
    return day, f"{hh}:{mi}"


def fetch_public_events(cal_id: str):
    """公開カレンダーの今後のイベントを (content_hash, [dict]) で返す。

    dict は TimeTree と同じ形: id/title/event_datetime/venue/ticket_url/note/page_url。
    """
    text = _get(ics_url(cal_id))
    if not text or "BEGIN:VCALENDAR" not in text:
        return None, []
    lines = _unfold(text)
    content_hash = hashlib.sha1(
        (EXTRACT_VERSION + "\n" + cal_id + "\n" + "\n".join(lines)).encode("utf-8", "ignore")
    ).hexdigest()

    today = datetime.datetime.now(JST).date()
    events = []
    cur = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                ev = _build(cur, cal_id, today)
                if ev:
                    events.append(ev)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.split(";", 1)[0].upper()
        params = name[len(key):]
        if key in ("UID", "SUMMARY", "LOCATION", "DESCRIPTION", "URL", "DTSTART", "STATUS"):
            cur[key] = (value, params)
    return content_hash, events


def _build(fields: dict, cal_id: str, today):
    if (fields.get("STATUS") or ("", ""))[0].upper() == "CANCELLED":
        return None
    raw_title = _unescape((fields.get("SUMMARY") or ("", ""))[0]).strip()
    if not raw_title:
        return None
    dtstart = fields.get("DTSTART")
    if not dtstart:
        return None
    day, hhmm = _parse_dt(dtstart[0], dtstart[1])
    if day is None or day < today:
        return None
    note = _unescape((fields.get("DESCRIPTION") or ("", ""))[0]).strip()
    location = _unescape((fields.get("LOCATION") or ("", ""))[0]).strip() or None
    link = (fields.get("URL") or ("", ""))[0].strip() or None
    # 先頭の分類タグ([Live]・【生誕】等)は公演名ではないので先に落とす。
    # 残したまま _core_name に渡すと「Live]AYNs」のように括弧だけが残る。
    stripped = re.sub(r"^\s*(?:[\[【(（][^\]】)）]{0,12}[\]】)）]\s*)+", "", raw_title)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    title = timetree._core_name(stripped) or stripped or raw_title
    ticket_url = timetree._ticket_url_from_note(note) or link
    # 「予定」「遠征予定」のような実体名の無い行は、メモの引用名で補えなければ
    # 取り込まない(TimeTree 側と同じ判断)。
    if timetree.looks_placeholder(stripped or raw_title):
        quoted = timetree._title_from_note(note)
        if quoted:
            title = quoted
        elif not ticket_url:
            return None
    uid = (fields.get("UID") or ("", ""))[0].strip()
    ev_id = hashlib.sha1(f"{cal_id}|{uid or raw_title}|{day}".encode("utf-8")).hexdigest()[:20]
    return {
        "id": ev_id,
        "title": title,
        "event_datetime": f"{day} {hhmm}" if hhmm else str(day),
        "venue": location,
        "ticket_url": ticket_url,
        "note": note or None,
        "page_url": None,
    }
