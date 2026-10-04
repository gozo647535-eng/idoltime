"""メモ(カレンダーの説明欄)から、そのグループの出演時刻と開場時刻を取り出す。

対バン・フェスでは「何時に出るのか」が知りたい情報だが、どのソースも
構造化された出演時刻を持たない。一方グループ自身のカレンダーのメモには
「🎙️出演19:35-20:00」「ライブ🎤14:45-15:05」「LIVE : 18:45-19:05」のように
書かれていることが多い(実測で396件)。

メモはそのカレンダーの持ち主のものなので、ここで取れる時刻はそのグループの
出演時刻として扱える。他グループへ相互反映した行(ttx:)には使わない。

LLM は使わない。
"""
import re

# 「出演 19:35-20:00」「ライブ🎤14:45-15:05」「LIVE : 18:45」
# 時刻の前に入る飾り(絵文字・空白・記号)は数文字だけ許す。
# 見出しと時刻の間に入れるのは飾り(絵文字・空白・記号)だけ。かな・漢字・英字を
# 許すと「出演するため18:50に特典会を終了」のような文まで拾ってしまう。
_GAP = r"[^\w\d\n\u3040-\u30ff\u4e00-\u9fff]{0,6}"
_HHMM = r"(\d{1,2})\s*[:：時]\s*(\d{2})"
_HEAD = r"(?:出演|ライブ|LIVE|ステージ|STAGE|ACT)(?:時間|時刻)?"
_STAGE = re.compile(
    _HEAD + _GAP + _HHMM + r"(?:\s*[-~〜–—]{1,2}\s*" + _HHMM + r")?",
    re.IGNORECASE)
# 「＜出演時間＞」だけの行(時刻は次の行)
_STAGE_HEADING = re.compile(r"^[^\w\d]{0,3}" + _HEAD + r"[^\w\d\n]{0,4}$", re.IGNORECASE)
# 行頭の素の時刻(見出しの次の行)
_BARE_TIME = re.compile(
    r"^[^\w\d]{0,3}" + _HHMM + r"(?:\s*[-~〜–—]{1,2}\s*" + _HHMM + r")?\s*$")

# 「OPEN 16:30」「OP.16:30」「開場16:30」
_OPEN = re.compile(
    r"(?:開場|OPEN|OP)\s*[.：:／/｜|]?\s*(\d{1,2})\s*[:：時]\s*(\d{2})",
    re.IGNORECASE)

# 出演時刻ではない行(物販・特典会・受付・配信は別の時刻)
_OTHER_SLOT = re.compile(r"(物販|特典会|受付|集合|開場|開演|配信|販売|発売)")


def _to_hhmm(h, mi):
    h, mi = int(h), int(mi)
    if h > 29 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def plain_lines(note):
    """メモを行に戻す(カレンダーのメモは HTML のことがある)。"""
    if not note:
        return []
    t = re.sub(r"(?i)<\s*br\s*/?>|</\s*p\s*>|</\s*div\s*>", "\n", note)
    t = re.sub(r"<[^>]+>", "", t)
    t = (t.replace("&amp;", "&").replace("&nbsp;", " ")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


def _span(m, i=1):
    start = _to_hhmm(m.group(i), m.group(i + 1))
    if not start:
        return None
    if m.group(i + 2):
        end = _to_hhmm(m.group(i + 2), m.group(i + 3))
        if end and end != start:
            return f"{start}-{end}"
    return start


def stage_time_from_note(note):
    """そのグループの出演時刻。「19:35-20:00」または「19:35」。読めなければ None。"""
    lines = plain_lines(note)
    for i, ln in enumerate(lines):
        m = _STAGE.search(ln)
        if m:
            # 「物販20:55-22:00」のように別の枠の時刻が先に書かれた行は見ない
            o = _OTHER_SLOT.search(ln)
            if o and o.start() < m.start():
                continue
            t = _span(m)
            if t:
                return t
        # 「＜出演時間＞」の次の行に時刻だけが書かれている形
        if _STAGE_HEADING.match(ln):
            for nxt in lines[i + 1:i + 3]:
                b = _BARE_TIME.match(nxt)
                if b:
                    t = _span(b)
                    if t:
                        return t
                if nxt:
                    break
    return None


def open_time_from_note(note):
    """開場時刻。読めなければ None。"""
    for ln in plain_lines(note):
        m = _OPEN.search(ln)
        if m:
            t = _to_hhmm(m.group(1), m.group(2))
            if t:
                return t
    return None
