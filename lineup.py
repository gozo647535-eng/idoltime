"""イベント本文から、図鑑に登録されているグループ名を拾い出す。

対バンやフェスは、どこか1組のカレンダー/サイトにしか載らないことが多い。
本文の出演欄に名前があるグループにも出演情報を出すため、ここで突合する。

精度が肝心なので、照合は「実際に書かれている形」で行う。記号や空白を落として
しまう強い正規化(ticketdive._norm_name)だけに頼ると、
  - URL の途中: tdv.c"om/ore"fes → MORE* に一致
  - 英語の定型句: "and more" → MORE* に一致
のような誤検出が出る。強い正規化は、表記揺れが大きい日本語名の保険としてのみ使う。
"""
import re
import unicodedata
from typing import List, Tuple

import ticketdive

_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")
# 正規化名がこれより短い名前は、他の語に偶然含まれやすいので突合に使わない
MIN_NAME_LEN = 4


def _light(s: str) -> str:
    """NFKC + 小文字化 + 空白の圧縮。記号は落とさない(名前の一部なので)。"""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", s or "").lower()).strip()


def _is_ascii_word(s: str) -> bool:
    return bool(s) and all(c.isascii() and c.isalnum() for c in s)


def build_index(idols) -> List[Tuple[str, str, str, str]]:
    """(強い正規化名, 実際の表記の軽い正規化, 表示名, slug) を長い順に。"""
    idx = []
    for i in idols:
        name = (i.get("name") or "").strip()
        slug = i.get("idol_slug") or i.get("slug")
        key = ticketdive._norm_name(name)
        if not name or not slug or len(key) < MIN_NAME_LEN:
            continue
        idx.append((key, _light(name), name, slug))
    idx.sort(key=lambda t: -len(t[0]))
    return idx


def _spans(hay: str, needle: str):
    out, s = [], 0
    while True:
        p = hay.find(needle, s)
        if p < 0:
            return out
        out.append((p, p + len(needle)))
        s = p + 1


def _light_spans(text: str, lit: str):
    """書かれている形での出現位置。英数字だけの名前は語境界を要求する。"""
    if not lit:
        return []
    if _is_ascii_word(lit):
        pat = r"(?<![0-9a-z])" + re.escape(lit) + r"(?![0-9a-z])"
        return [(m.start(), m.end()) for m in re.finditer(pat, text)]
    return _spans(text, lit)


def names_in_text(text: str, idx) -> List[Tuple[str, str]]:
    """本文に出てくる登録グループ [(表示名, slug), ...]。

    長い名前を先に見て、既に採った長い名前の内側にしか現れない短い名前は採らない
    (「MORE STAR」の中の「MORE*」のような重複を避ける)。
    """
    if not text:
        return []
    plain = _URL_RE.sub(" ", text)  # URL の中の文字列は名前ではない
    lt = _light(plain)
    nt = ticketdive._norm_name(plain)
    found, taken_l, taken_n = [], [], []
    for key, lit, disp, slug in idx:
        sp = _light_spans(lt, lit)
        taken = taken_l
        if not sp and not _is_ascii_word(lit) and any(not c.isascii() for c in key):
            # 日本語名のみ、記号・空白の揺れを吸収する強い正規化で再照合
            sp, taken = _spans(nt, key), taken_n
        if not sp:
            continue
        if all(any(a <= s and e <= b for a, b in taken) for s, e in sp):
            continue
        taken.extend(sp)
        found.append((disp, slug))
    return found


# 出演者欄を日ごとに分けて書く投稿(2日開催のフェス)の、日付だけの見出し行。
# 「11/21」「【11/22(日)】」「2026/11/22(日)」「11月21日(土) 出演」など
_DAY_HEAD = re.compile(
    r"^\s*[【\[［(（<＜■◆●▼◇☆★]*\s*(?:20\d\d\s*[/.年-]\s*)?(\d{1,2})\s*[/月.]\s*(\d{1,2})\s*日?"
    r"\s*(?:[(（][^)）]{1,6}[)）])?\s*[】\]］)）>＞]?\s*(?:出演(?:者)?)?\s*[:：]?\s*(.*)$"
)
# 見出しの後ろに続いてよいのは出演者の並びだけ。時刻や販売の案内が続く行は見出しではない
_NOT_LINEUP = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}|開場|開演|販売|発売|受付|締切|〆|抽選|発表|まで|~|〜|～")


def day_sections(text: str, idx) -> List[Tuple[str, str]]:
    """本文を日付の見出しで区切る [(\"MM-DD\" または \"\", その区切りの本文), ...]。

    日付の見出しが2つ以上ある(日ごとに出演者を書き分けている)ときだけ区切る。
    そうでなければ空を返す。見出しより前と、見出しの段落が空行で終わったあとは
    \"\"(日付の指定なし)。「11/28 ライブ 18:50〜」のように日ごとの出演時刻を書いた
    段落のあとに、両日の出演者をまとめて並べる投稿があるため、見出しの効き目は
    その段落の中だけにする。
    """
    if not text:
        return []
    sections, cur, buf, heads = [], "", [], set()
    for line in unicodedata.normalize("NFKC", text).split("\n"):
        if not line.strip():
            # 見出しの直後の空行は段落の終わりではない(「11/21」の次に1行空けて並べる書き方)
            if cur and any(x.strip() for x in buf):
                sections.append((cur, "\n".join(buf)))
                cur, buf = "", []
            buf.append(line)
            continue
        m = _DAY_HEAD.match(line)
        rest = m.group(3).strip() if m else ""
        is_head = bool(m) and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31 and (
            not rest or (not _NOT_LINEUP.search(rest) and names_in_text(rest, idx))
        )
        if not is_head:
            buf.append(line)
            continue
        sections.append((cur, "\n".join(buf)))
        cur = f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        heads.add(cur)
        buf = [rest]
    sections.append((cur, "\n".join(buf)))
    return sections if len(heads) >= 2 else []
