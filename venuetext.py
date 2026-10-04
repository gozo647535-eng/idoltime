"""メモ(カレンダーの説明欄・TimeTree のメモ)から会場名を取り出す。

TimeTree・Googleカレンダーの会場欄(location)は空のことが多く(実測で
TimeTree 52%・Googleカレンダー 90%)、代わりにメモへ「会場：〇〇」「📍〇〇」と
書かれている。都道府県での絞り込みや会場ページは会場名が無いと働かないので、
メモから拾う。

LLM は使わない。確実な書き方だけを拾い、判断できないものは None を返す。
"""
import re

# 「会場：〇〇」「場所: 〇〇」「VENUE / 〇〇」。
# 「会場内のエリア図は…」のような続きの文を拾わないよう、区切り記号か空白を必須にし、
# 「会場内」「会場図」「会場周辺」は見出しではないので外す。
_LABEL = re.compile(
    r"(?:会場|場所|開催地|開催場所|venue|place)"
    r"(?!内|外|図|周辺|付近|案内|map)"
    r"\s*(?:[:：=＝／/｜|]\s*|[\s　]+)(\S.*)",
    re.IGNORECASE)
# 会場名が助詞で始まることはない(「会場 の詳細は…」を弾く)
_PARTICLE_START = re.compile(r"^(?:の|は|が|を|に|で|と|へ|も|や|から|まで|について|内)")
# 「📍〇〇」「🏟〇〇」。先頭の装飾を1〜2文字許す
_PIN = re.compile(r"^.{0,2}?(?:📍|📌|🏟|🏢|🎪|🏛|⛩)️?\s*(\S.*)$")

# 会場名らしさ。この語を含む行は会場として採ってよい
_VENUE_WORD_JA = re.compile(
    r"(ホール|会館|公会堂|劇場|シアター|アリーナ|ドーム|スタジアム|スタジオ|"
    r"ライブハウス|野音|音楽堂|体育館|公園|神社|神宮|記念館|美術館|博物館|"
    r"タワーレコード|タワレコ|ベルサール|プラザ|センター|キューブ|百貨店)")
# ラテン文字の語は前後を区切る。区切らないと HALLOWEEN が HALL に当たってしまう
_VENUE_WORD_EN = re.compile(
    r"(?<![A-Za-z])(THEATERS?|THEATRES?|HALLS?|ARENA|DOME|STUDIOS?|"
    r"LIVE\s?HOUSE|LIVEHOUSE|ZEPP|CLUB|LOUNGE|TOWER\s?RECORDS|HMV|"
    r"CAFES?|BAR|ROOMS?|STAGE|PARK|SQUARE)(?![A-Za-z])", re.IGNORECASE)

# 会場ではない行(日時・料金・チケット・出演・URL)
_NOT_VENUE = re.compile(
    r"(https?://|^\s*[#＃]|チケット|ticket|料金|前売|当日|円|¥|￥|\d+:\d{2}|"
    r"OPEN|OPEN/|START|OP\b|ST\b|開場|開演|物販|特典|出演|配信|発売|受付|"
    r"予約|販売|問い合わせ|主催|企画|詳細|お知らせ|住所)",
    re.IGNORECASE)
_DATE_LINE = re.compile(r"\d{1,2}\s*[/月]\s*\d{1,2}|20\d\d\s*[/年]")

# 行末に付く補足(「〇〇ホール ※1部」「〇〇 (東京)」は会場名まで)
_TAIL = re.compile(r"\s*(?:※.*|（[^）]*開場[^）]*）|\([^)]*開場[^)]*\))$")


def _clean(s: str) -> str:
    # 「【会場】Zephyr Hall」の見出し、「】タワーレコード…」のような括弧の残り
    s = re.sub(r"^[\s　]*[【\[(（]\s*(?:会場|場所|venue)\s*[】\])）]\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^[\s　]*[】）)］\]＞>｝}]+", "", s)
    s = _TAIL.sub("", s).strip()
    s = re.sub(r"\s*[/｜|]\s*$", "", s)
    s = s.strip(" 　・:：-ー–—")
    # 行の途中から別の情報が始まるものは会場名まで切る
    s = re.split(r"\s*(?:OPEN|OP\.?|START|ST\.?|開場|開演|時間|チケット)\s*[:：]?\s*\d",
                 s, 1, re.IGNORECASE)[0]
    return s.strip()


def _plausible(name: str) -> bool:
    if not name or len(name) < 2 or len(name) > 60:
        return False
    if _NOT_VENUE.search(name) or _DATE_LINE.search(name):
        return False
    # 「t-dv.com/…」のように scheme の無いURLも会場ではない
    if re.search(r"[\w-]+\.(?:com|net|jp|io|me|tv|co)(?:/|$)", name, re.IGNORECASE):
        return False
    if _PARTICLE_START.match(name):
        return False
    # 「5MAN🎤開催決定】」のような告知の断片
    if re.search(r"(決定|開催|予定|情報|詳細|公開|追加)\s*[】）\)]?$|^[^\w]*\d+MAN", name):
        return False
    # 1文字の記号列や、数字だけのものは会場ではない
    if not re.search(r"[0-9A-Za-z぀-ヿ一-鿿]", name):
        return False
    return True


def _html_to_lines(note: str):
    """カレンダーのメモは HTML のことがある(<p>…</p><br>)。行に戻す。"""
    t = re.sub(r"(?i)<\s*br\s*/?>|</\s*p\s*>|</\s*div\s*>", "\n", note)
    t = re.sub(r"<[^>]+>", "", t)
    t = (t.replace("&amp;", "&").replace("&nbsp;", " ")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return [ln.strip() for ln in t.split("\n")]


def _looks_like_title(name: str, title) -> bool:
    """公演名そのもの(「fishbowl ライブハウスショートツアー「ケンガイ2026」」)は会場ではない。"""
    if re.search(r"[「」『』]|ツアー|TOUR|presents|vol\.?\s*\d|第\d+回", name, re.IGNORECASE):
        return True
    if not title:
        return False
    a = re.sub(r"[\s　]+", "", name)
    b = re.sub(r"[\s　]+", "", title)
    return bool(a) and (a in b or b in a)


def venue_from_note(note, title=None):
    """メモから会場名を取り出す。確実に読めないときは None。"""
    if not note:
        return None
    lines = [ln for ln in _html_to_lines(note) if ln]
    if not lines:
        return None
    # ① 「会場：〇〇」。いちばん確実
    for ln in lines:
        m = _LABEL.search(ln)
        if m:
            name = _clean(m.group(1))
            if _plausible(name):
                return name
    # ② 「📍〇〇」
    for ln in lines:
        m = _PIN.match(ln)
        if m:
            name = _clean(m.group(1))
            if _plausible(name):
                return name
    # ③ メモが会場名だけ(「Spotify O-WEST」)
    if len(lines) == 1:
        name = _clean(lines[0])
        if _plausible(name) and len(name) <= 30 and not _looks_like_title(name, title):
            return name
    # ④ 会場名らしい語を含む行(「パシフィコ横浜国立大ホール」)
    for ln in lines:
        name = _clean(ln)
        if not _plausible(name) or len(name) > 40:
            continue
        if _looks_like_title(name, title):
            continue
        if _VENUE_WORD_JA.search(name) or _VENUE_WORD_EN.search(name):
            return name
    return None
