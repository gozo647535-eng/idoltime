"""TimeTree 公開カレンダーからのイベント/チケット情報取得。

一部のアイドルは公演スケジュールを TimeTree の「公開カレンダー」で配信している。
公開カレンダーは構造化された JSON API から取得できるため、スクレイピングより堅牢で、
公演日時・会場・メモ(チケット情報)を確実に得られる。認証は不要
(Web版と同じ固定ヘッダ X-TimeTreeA のみ)。

公演メモ(note)には発売日時やチケットURLが自由記述で含まれることが多いので、
チケット関連キーワードを含む場合のみ Gemini で発売日時/先着判定を補完する
(入力が短いためコストは小さい)。
"""
import datetime
import gzip
import hashlib
import json
import re
import unicodedata
import urllib.request
from typing import List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

import ticketdive  # 名前の正規化(装飾記号を落とす)を共用する

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
# Web クライアントが送る固定ヘッダ。値は "web/<ver>/<locale>" 形式であれば良い。
API_HEADER = {"X-TimeTreeA": "web/1.0.0/ja", "User-Agent": UA, "Accept": "application/json"}

JST = datetime.timezone(datetime.timedelta(hours=9))

# メモ抽出ロジックを変えたらこの値を上げる(既読カレンダーの再処理をトリガー)
# v3: TicketDive 構造化データによる補完を追加
# v5: メモから開場/入場時刻(open_time/entry_time)を抽出
EXTRACT_VERSION = "v6"  # 会場欄が空の予定はメモから会場名を拾うようにしたので再取り込み

# チケット関連のメモか判定するキーワード
_TICKET_KW = ("チケット", "発売", "受付", "先行", "抽選", "予約", "販売", "先着")

# --- イベント名の整形/プレースホルダ判定 ---
# 前後の絵文字・記号を落として表示用の芯を取り出す(\w はPython3では日本語も含む)
_NAME_HEAD = re.compile(r"^[^\w【（「『]+")
_NAME_TAIL = re.compile(r"[^\w】）」』!?！？]+$")
# メモ内の 「」『』 に囲まれた実イベント名
_QUOTED_RE = re.compile(r"[「『]([^」』\n]{3,50})[」』]")
# 実体名の無い"予定"系プレースホルダ(例: ライブ予定 / 重要イベント予定 / 大阪遠征 / 遠征予定)
_PLACEHOLDER_RE = re.compile(r"^(?:重要)?(?:LIVE|ライブ|イベント|ワンマン)?(?:予定|未定)$", re.IGNORECASE)
_TABI_RE = re.compile(r"^.{0,5}遠征(?:予定)?$")
_PLACEHOLDER_WORDS = {"ライブ", "live", "出演", "イベント", "予定", "未定", "tba"}


def _core_name(title: str) -> str:
    """前後の絵文字・装飾記号を除いた表示用イベント名。"""
    t = _NAME_TAIL.sub("", _NAME_HEAD.sub("", title or ""))
    return t.strip()


def looks_placeholder(title: str) -> bool:
    """実イベント名を持たない汎用的な"予定"表記なら True。"""
    core = _core_name(title)
    body = re.sub(r"[（(【][^）)】]*[）)】]", "", core).strip()  # (出演日未定)等の括弧書きを除く
    norm = re.sub(r"\s|　", "", body)
    if not norm:
        return True
    if norm.lower() in _PLACEHOLDER_WORDS:
        return True
    return bool(_PLACEHOLDER_RE.match(norm) or _TABI_RE.match(norm))


def _title_from_note(note: str):
    m = _QUOTED_RE.search(note or "")
    return m.group(1).strip() if m else None

# 発売/受付日時を表す語。この直後に M/D [HH:MM] が続く書式を正規表現で拾う。
_SALE_KW = r"(?:先着発売|一般発売|一般販売|先行発売|発売|受付開始|受付|先行|抽選|販売開始|申込開始)"
_SALE_RE = re.compile(
    _SALE_KW + r"[^\d\n]{0,8}(\d{1,2})[/月](\d{1,2})(?:日)?[^\d\n]{0,6}(?:(\d{1,2})[:：](\d{2}))?"
)
# メモ本文からチケットURLを拾う(短縮ドメインも含む)
_TICKET_URL_RE = re.compile(
    r"https?://[^\s　]*(?:ticketdive\.com|t-dv\.com|eplus\.jp|t\.pia\.jp|pia\.jp|"
    r"l-tike\.com|livepocket\.jp|t\.livepocket\.jp|tiget\.net)[^\s　]*",
    re.IGNORECASE,
)
# t-dv.com/xxx のような "https://" が省略された短縮表記も拾う
_TICKET_URL_BARE_RE = re.compile(
    r"(?<![\w/])(?:t-dv\.com|tiget\.net|l-tike\.com)/[^\s　]+", re.IGNORECASE
)


# 開場/入場時刻を自由文から拾う(「開場18:00」「開場 18時00分」「開場:18:00」等)
_OPEN_TIME_RE = re.compile(r"開場\s*[:：]?\s*(\d{1,2})\s*[:：時]\s*(\d{2})")
_ENTRY_TIME_RE = re.compile(r"入場\s*[:：]?\s*(\d{1,2})\s*[:：時]\s*(\d{2})")


def _fmt_hhmm(m) -> Optional[str]:
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 29 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def extract_open_entry(*texts: Optional[str]):
    """テキスト群から開場/入場時刻(HH:MM)を拾う。見つからなければ None。
    返り値 (open_time|None, entry_time|None)。複数ソース(メモ・本文)を順に見る。"""
    open_t = entry_t = None
    for t in texts:
        if not t:
            continue
        if open_t is None:
            open_t = _fmt_hhmm(_OPEN_TIME_RE.search(t))
        if entry_t is None:
            entry_t = _fmt_hhmm(_ENTRY_TIME_RE.search(t))
        if open_t and entry_t:
            break
    return open_t, entry_t


def _get(url: str, timeout: int = 20) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={**API_HEADER, "Accept-Encoding": "gzip"})
        r = urllib.request.urlopen(req, timeout=timeout)
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data
    except Exception as e:
        print(f"    TimeTree 取得失敗 {url}: {e}")
        return None


def extract_alias(url: str) -> Optional[str]:
    """TimeTree のリンクから公開カレンダーの alias_code を取り出す。
    対応: timetr.ee/pc/<alias> / timetreeapp.com/public_calendars/<alias> /
    timetr.ee/<code>(リダイレクトを1回たどって解決)。"""
    if not url:
        return None
    m = re.search(r"public_calendars/([A-Za-z0-9_.-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"timetr\.ee/pc/([A-Za-z0-9_.-]+)", url)
    if m:
        return m.group(1)
    host = urlparse(url).netloc.lower()
    if "timetr.ee" in host:
        # 短縮URL: リダイレクト先(public_calendars/<alias>)を解決する
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            r = urllib.request.urlopen(req, timeout=15)
            m = re.search(r"public_calendars/([A-Za-z0-9_.-]+)", r.geturl())
            if m:
                return m.group(1)
        except Exception as e:
            print(f"    TimeTree alias 解決失敗 {url}: {e}")
    return None


def _event_datetime(ev: dict) -> Optional[str]:
    """public_event を表示用の日時文字列(JST基準)に変換。
    終日イベントは 'YYYY-MM-DD'、時刻ありは 'YYYY-MM-DD HH:MM'。"""
    start = ev.get("start_at")
    if not isinstance(start, (int, float)):
        return None
    if ev.get("all_day"):
        # 終日: start_at は該当日の 00:00 UTC。UTCの日付がそのままJSTの暦日。
        d = datetime.datetime.fromtimestamp(start / 1000, datetime.timezone.utc)
        return d.strftime("%Y-%m-%d")
    dt = datetime.datetime.fromtimestamp(start / 1000, datetime.timezone.utc).astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M")


def _event_date_only(ev: dict) -> Optional[datetime.date]:
    start = ev.get("start_at")
    if not isinstance(start, (int, float)):
        return None
    if ev.get("all_day"):
        return datetime.datetime.fromtimestamp(start / 1000, datetime.timezone.utc).date()
    return datetime.datetime.fromtimestamp(start / 1000, datetime.timezone.utc).astimezone(JST).date()


def _ticket_url_from_note(note: str) -> Optional[str]:
    if not note:
        return None
    m = _TICKET_URL_RE.search(note)
    if m:
        return m.group(0).rstrip("。、）)")
    m = _TICKET_URL_BARE_RE.search(note)
    if m:
        return "https://" + m.group(0).rstrip("。、）)")
    return None


class NoteTicket(BaseModel):
    sale_datetime: Optional[str] = Field(
        None, description="チケットの発売/受付/先行/抽選申込の開始日時。分かる場合のみ 'YYYY-MM-DD HH:MM:SS'(年不明なら当年、秒は00)")
    is_first_come: bool = Field(False, description="先着/一般発売(先着)なら True。抽選・先行(抽選)は False")


def _regex_note_ticket(note: str, today: datetime.date):
    """メモ本文から発売日時と先着判定を正規表現で抽出(無料・Gemini不要)。
    返り値 (sale_datetime_str|None 'YYYY-MM-DD HH:MM:SS', is_first_come_bool)。"""
    has_fc = ("先着" in note) or ("一般発売" in note) or ("一般販売" in note)
    has_lot = "抽選" in note  # 抽選が絡むなら先着扱いにしない
    is_fc = has_fc and not has_lot
    m = _SALE_RE.search(note)
    if not m:
        return None, is_fc
    mo, d, h, mi = m.groups()
    try:
        cand = datetime.date(today.year, int(mo), int(d))
    except ValueError:
        return None, is_fc
    # 年跨ぎ対策: 極端に過去なら翌年とみなす(12月→1月など)
    if cand < today - datetime.timedelta(days=60):
        cand = cand.replace(year=today.year + 1)
    h = int(h) if h else 0
    mi = int(mi) if mi else 0
    return f"{cand.isoformat()} {h:02d}:{mi:02d}:00", is_fc


def _gemini_note_ticket(gemini_client, title: str, note: str, today: str):
    """正規表現で取れなかった場合のフォールバック。Gemini で発売日時を抽出。
    クォータ切れ等で失敗しても (None, False) を返すだけで害はない。"""
    if not gemini_client:
        return None, False
    # 本日の無料枠(2026-10 時点で 20リクエスト/日)を使い切っていたら呼ばない。
    # ここに短絡が無かったため、枠切れ後もメモ1件ごとに429を取りに行き、
    # 間隔制御の待ちだけで実行時間を食い潰していた(run #141 が55分で打ち切り)。
    try:
        import sites
        if sites._quota_exhausted[0]:
            return None, False
    except Exception:
        pass
    from google.genai import types

    prompt = (
        f"本日は {today}(JST)。以下はライブ「{title}」のメモです。"
        "チケットの発売・受付・先行・抽選申込の開始日時が書かれていれば抽出してください。"
        "発売情報が無ければ sale_datetime は null。\n\nメモ:\n" + note[:1200]
    )
    try:
        # Gemini 無料枠のレート制限を守る(sites 側と同じ間隔制御を共用)
        try:
            import sites
            sites._gemini_throttle()
        except Exception:
            pass
        res = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=NoteTicket,
                temperature=0.1,
            ),
        )
        nt = res.parsed
        return nt.sale_datetime, bool(nt.is_first_come)
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            # 日次枠切れ。以降このプロセスでは Gemini を呼ばず正規表現の結果を使う。
            try:
                import sites
                if not sites._quota_exhausted[0]:
                    sites._quota_exhausted[0] = True
                    print("    Gemini の日次枠切れ。以降メモの解析は正規表現のみで行います")
            except Exception:
                pass
            return None, False
        print(f"    TimeTree メモGemini解析失敗(正規表現の結果を使用): {e}")
        return None, False


def extract_note_ticket(gemini_client, title: str, note: str, today: str):
    """メモから発売日時/先着を抽出。まず正規表現(無料)、取れなければGeminiで補完。
    返り値 (sale_datetime_str|None, is_first_come_bool)。today は 'YYYY-MM-DD'。"""
    if not note or not any(k in note for k in _TICKET_KW):
        return None, False
    today_date = datetime.date.fromisoformat(today)
    sale, is_fc = _regex_note_ticket(note, today_date)
    if sale:  # 正規表現で取れたら Gemini は呼ばない(コスト節約・クォータ非依存)
        return sale, is_fc
    # 取れない時だけ Gemini にフォールバック
    g_sale, g_fc = _gemini_note_ticket(gemini_client, title, note, today)
    return g_sale, (is_fc or g_fc)


def fetch_public_events(alias: str):
    """公開カレンダーのイベントを取得。(content_hash, [dict]) を返す。失敗時 (None, [])。
    dict は upcoming のみ・整形済み: id/title/event_datetime/venue/ticket_url/note/page_url。"""
    url = f"https://timetreeapp.com/api/v2/public_calendars/{alias}/public_events"
    data = _get(url)
    if not data:
        return None, []
    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except Exception as e:
        print(f"    TimeTree JSON 解析失敗 {alias}: {e}")
        return None, []
    raw = payload.get("public_events") or []
    content_hash = hashlib.sha1(
        (EXTRACT_VERSION + "\n" + alias + "\n" + json.dumps(raw, ensure_ascii=False, sort_keys=True)).encode("utf-8", "ignore")
    ).hexdigest()

    today = datetime.datetime.now(JST).date()
    events = []
    for ev in raw:
        d = _event_date_only(ev)
        if d is None or d < today:  # 過去は除外
            continue
        raw_title = (ev.get("title") or "").strip()
        if not raw_title:
            continue
        note = ev.get("note") or ""
        ticket_url = _ticket_url_from_note(note) or (ev.get("link_url") or "").strip() or None
        # イベント名の整形: 絵文字/装飾を除去。プレースホルダ(「ライブ予定」等)は
        # メモの引用名で補完し、それも無く手掛かりが無ければ取り込まない。
        title = _core_name(raw_title) or raw_title
        if looks_placeholder(raw_title):
            quoted = _title_from_note(note)
            if quoted:
                title = quoted
            elif not ticket_url:
                # 実体名もチケットも無い漠然とした予定 → ノイズなのでスキップ
                continue
            # チケットがあれば残す(TicketDive補完で正式名が入る)
        venue = (ev.get("location_name") or ev.get("location_address") or "").strip() or None
        events.append({
            "id": str(ev.get("id")),
            "title": title,
            "event_datetime": _event_datetime(ev),
            "venue": venue,
            "note": note,
            "ticket_url": ticket_url,
            "page_url": (ev.get("url") or "").strip() or None,
        })
    return content_hash, events

# --- 公開カレンダーの持ち主の確認 -------------------------------------------
# 自動発見したカレンダーをそのまま登録すると、名前が似た別グループや非公式の
# カレンダーを取り込んでしまう(Appare! に appare☆ず のカレンダーが入っていた)。
# カレンダー名から定型の飾りを落として、グループ名と一致するものだけ通す。
_CAL_DECOR = re.compile(
    r"(公式|オフィシャル|official|公開|スケジュール|schedule|カレンダー|calendar|ライブ|live|"
    r"イベント|event|情報|予定|日程|お知らせ|info|フォロー|更新|最新)", re.IGNORECASE)
_CAL_REJECT = re.compile(r"(非公式|unofficial|ファン|fan\b|個人|私用|勉強|仕事|バイト)", re.IGNORECASE)


def calendar_name(alias: str):
    """公開カレンダーの名前(ページの <title>)。取れなければ None。"""
    # _get は公開イベントAPI用のヘッダ(X-TimeTreeA / Accept: application/json)を
    # 送るため、HTMLのページには 404 を返される。ここはブラウザとして取る。
    url = f"https://timetreeapp.com/public_calendars/{alias}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        r = urllib.request.urlopen(req, timeout=20)
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    except Exception as e:
        print(f"    TimeTree カレンダー名の取得失敗 {alias}: {e}")
        return None
    html = raw.decode("utf-8", "replace")
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).replace(" - TimeTree", "").strip()


# 名前と同じ意味で使われる引用符の揺れ。Ma'Scar'Piece のカレンダー名は
# 「Ma’Scar’Piece Official」で、アポストロフィが U+2019 になっている。
_CAL_QUOTES = re.compile(r"['\u2018\u2019\u02bc\u00b4\u0060\u201c\u201d\"]")


# 読みや補足を囲む括弧。THE ENCORE のカレンダー名は「THE ENCORE【アンコール】」。
# 【非公式】の判定は括弧を外す前の名前に対して行うので、ここで落として差し支えない。
_CAL_BRACKETS = re.compile(r"[【（(\[［][^】）)\]］]{0,20}[】）)\]］]")


def _cal_core(s: str) -> str:
    s = re.sub(r"&#\d+;", "'", s or "")
    s = _CAL_QUOTES.sub("", unicodedata.normalize("NFKC", s))
    s = _CAL_BRACKETS.sub("", s)
    return ticketdive._norm_name(_CAL_DECOR.sub("", s))


# 名前と飾りの区切りに使われる記号・空白。「zanka｜イベントカレンダー」のように
# 飾り語を落とすと区切りだけが残るため、比較の前に落とす。
_CAL_SQUASH = re.compile(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]")


def calendar_belongs_to(group_name: str, cal_title: str) -> bool:
    """カレンダー名がそのグループのものと言えるか(飾りを除いて完全一致)。"""
    if not cal_title or _CAL_REJECT.search(cal_title):
        return False
    nm = _CAL_SQUASH.sub("", _cal_core(group_name))
    ct = _CAL_SQUASH.sub("", _cal_core(cal_title))
    return bool(nm) and len(nm) >= 3 and nm == ct
