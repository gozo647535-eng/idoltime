"""メモ(カレンダーの説明欄・TimeTree のメモ)から会場名を取り出す。

TimeTree・Googleカレンダーの会場欄(location)は空のことが多く(実測で
TimeTree 52%・Googleカレンダー 90%)、代わりにメモへ「会場：〇〇」「📍〇〇」と
書かれている。都道府県での絞り込みや会場ページは会場名が無いと働かないので、
メモから拾う。

LLM は使わない。確実な書き方だけを拾い、判断できないものは None を返す。
"""
import re
import unicodedata

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


# --- 場所の分類と、会場名の照合(仮タイトルの対応づけ用) ---
# 広域: 同じ公演の根拠にならない広さの場所
_WIDE_PLACES = frozenset({
    "都内", "都心", "首都圏", "国内", "全国", "未定", "調整中", "遠征",
    "関東", "関西", "東海", "東北", "北陸", "甲信越", "近畿", "中国地方", "四国", "九州", "沖縄", "北海道",
    "東京", "東京都", "大阪", "大阪府", "京都", "京都府", "青森", "青森県", "岩手", "岩手県",
    "宮城", "宮城県", "秋田", "秋田県", "山形", "山形県", "福島", "福島県", "茨城", "茨城県",
    "栃木", "栃木県", "群馬", "群馬県", "埼玉", "埼玉県", "千葉", "千葉県", "神奈川", "神奈川県",
    "新潟", "新潟県", "富山", "富山県", "石川", "石川県", "福井", "福井県", "山梨", "山梨県",
    "長野", "長野県", "岐阜", "岐阜県", "静岡", "静岡県", "愛知", "愛知県", "三重", "三重県",
    "滋賀", "滋賀県", "兵庫", "兵庫県", "奈良", "奈良県", "和歌山", "和歌山県", "鳥取", "鳥取県",
    "島根", "島根県", "岡山", "岡山県", "広島", "広島県", "山口", "山口県", "徳島", "徳島県",
    "香川", "香川県", "愛媛", "愛媛県", "高知", "高知県", "福岡", "福岡県", "佐賀", "佐賀県",
    "長崎", "長崎県", "熊本", "熊本県", "大分", "大分県", "宮崎", "宮崎県", "鹿児島", "鹿児島県",
    "沖縄県",
})
# エリア: 代表表記 → 別表記。会場名の先頭にも付くので、照合の前に別に取り除く
_AREAS = {
    "渋谷": ("shibuya",), "新宿": ("shinjuku",), "池袋": ("ikebukuro",),
    "下北沢": ("shimokitazawa", "下北"), "秋葉原": ("akihabara", "アキバ"), "原宿": ("harajuku",),
    "代官山": ("daikanyama",), "恵比寿": ("ebisu",), "高円寺": ("koenji",), "吉祥寺": ("kichijoji",),
    "上野": ("ueno",), "六本木": ("roppongi",), "赤坂": ("akasaka",), "目黒": ("meguro",),
    "品川": ("shinagawa",), "お台場": ("odaiba",), "横浜": ("yokohama",), "川崎": ("kawasaki",),
    "梅田": ("umeda",), "心斎橋": ("shinsaibashi",), "難波": ("なんば", "namba", "nanba"),
    "江坂": ("esaka",), "天王寺": ("tennoji",), "名古屋": ("nagoya",), "神戸": ("kobe",),
    "博多": ("hakata",), "天神": ("tenjin",), "札幌": ("sapporo",), "仙台": ("sendai",),
}
_AREA_OF = {v: k for k, vs in _AREAS.items() for v in (k,) + vs}
_AREA_HEADS = sorted(_AREA_OF, key=len, reverse=True)
# 会場名の先頭に付いたり付かなかったりする定型語
_VENUE_NOISE_HEADS = ("spotify", "ライブハウス", "livehouse")
_VENUE_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
# 同じ会場とみなすのに必要な長さ。短いキーは別の会場とも重なりやすい
_VENUE_KEY_MIN = 4
# 公演名の末尾に書かれた場所(「『アイドル道場 Vol. 71』@渋谷VIDENT」「あいらんど（新宿ReNY）」)
_TITLE_TAIL_PLACE = re.compile(r"(?:[@📍📌]️?\s*([^@📍📌()]+)|\(([^()]+)\)?)\s*$")


def _squash_place(s) -> str:
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", s or "")).lower()


def place_kind(place) -> str:
    """場所の分類: none / wide(広域) / area(エリア) / venue(会場)。広域・エリアは完全一致のみ。"""
    p = _squash_place(place)
    if not p:
        return "none"
    if p in _WIDE_PLACES:
        return "wide"
    return "area" if p in _AREA_OF else "venue"


def venue_key(name, aliases=None):
    """会場名を (照合キー, エリア) にする。部分一致を避けるため、照合はキーの完全一致で行う。"""
    s = _VENUE_TRAILING_PAREN.sub("", unicodedata.normalize("NFKC", name or "").lower())  # 住所の括弧
    s = re.sub(r"[\W_]+", "", s)
    area = None
    stripped = True
    while stripped:
        stripped = False
        for head in _VENUE_NOISE_HEADS:
            if s.startswith(head):
                s, stripped = s[len(head):], True
        for head in _AREA_HEADS:
            if s.startswith(head):
                area = area or _AREA_OF[head]
                s, stripped = s[len(head):], True
                break
    return (aliases or {}).get(s, s), area


def venue_alias_map(registry) -> dict:
    """venues.json の別名 → 同じ会場を指すキー。"""
    out = {}
    for v in registry or []:
        for n in [v.get("name")] + list(v.get("aliases") or []):
            key = venue_key(n)[0]
            if key and v.get("id"):
                out[key] = f"id:{v['id']}"
    return out


def same_venue(a, b, aliases=None) -> bool:
    """正規化後の完全一致で同じ会場か。両方にエリアがあれば同じエリアであることも要求する。"""
    if place_kind(a) != "venue" or place_kind(b) != "venue":
        return False
    ka, area_a = venue_key(a, aliases)
    kb, area_b = venue_key(b, aliases)
    if ka != kb or len(ka) < _VENUE_KEY_MIN:
        return False
    return not (area_a and area_b and area_a != area_b)


def place_area(place):
    """場所のエリア(代表表記)。分からなければ None。"""
    kind = place_kind(place)
    if kind == "area":
        return _AREA_OF[_squash_place(place)]
    return venue_key(place)[1] if kind == "venue" else None


def places_conflict(a, b, aliases=None) -> bool:
    """2つの場所が明らかに食い違うか。広域・場所なし・エリア不明は判断できないので食い違いとしない。"""
    ka, kb = place_kind(a), place_kind(b)
    if ka in ("none", "wide") or kb in ("none", "wide"):
        return False
    if ka == "venue" and kb == "venue":
        return not same_venue(a, b, aliases)
    area_a, area_b = place_area(a), place_area(b)
    return bool(area_a and area_b and area_a != area_b)


def place_from_title(title):
    """公演名の末尾に書かれた場所。無ければ None。"""
    m = _TITLE_TAIL_PLACE.search(unicodedata.normalize("NFKC", title or "").strip())
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None
