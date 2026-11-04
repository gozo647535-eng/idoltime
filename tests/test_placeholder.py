"""仮タイトルの判定・段0の照合(DB を変えない)・第1段の書き換えのテスト。

ネットワークにも Supabase にも触れない。sb と取得関数はすべて差し替える。
"""
import datetime
import json
import os
from unittest.mock import MagicMock

import pytest

# worker / sb は import 時に環境変数を読むので、実環境の設定を持ち込まない
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "YRT_FORCE"):
    os.environ.pop(_key, None)

import gcal  # noqa: E402
import timetree  # noqa: E402
import venuetext  # noqa: E402
import worker  # noqa: E402

SLUG = "artist:テスト"
NAME = "テスト"
DAY = "2099-11-04"
REAL = "アイドル道場 Vol.71"


# --- 仮タイトルの判定 ---

@pytest.mark.parametrize("title, kind, place, marked", [
    ("ライブ予定", "ライブ", None, False),
    ("LIVE予定", "live", None, False),
    ("出演予定", "出演", None, False),
    ("イベント予定", "イベント", None, False),
    ("主催予定", "主催", None, False),
    ("🎤ライブ予定🎤", "ライブ", None, False),
    ("ライブ予定（仮）", "ライブ", None, True),
    ("ライブ予定(仮)", "ライブ", None, True),
    ("ライブ予定(仮", "ライブ", None, True),   # 保存済みの古い行の形
    ("ライブ予定@都内", "ライブ", "都内", False),
    ("ライブ予定＠都内", "ライブ", "都内", False),
    ("ライブ予定(仮)@都内", "ライブ", "都内", True),
    ("ライブ予定@都内(仮)", "ライブ", "都内", True),
    ("出演予定📍渋谷", "出演", "渋谷", False),
    ("出演予定📌渋谷", "出演", "渋谷", False),
    ("出演予定@渋谷", "出演", "渋谷", False),
    ("ライブ予定@渋谷VIDENT", "ライブ", "渋谷VIDENT", False),
    ("ライブ予定@Spotify O-nest", "ライブ", "Spotify O-nest", False),
])
def test_tentative_titles_match(title, kind, place, marked):
    assert timetree.parse_tentative_title(title) == {"kind": kind, "place": place, "marked": marked}


@pytest.mark.parametrize("title", [
    "ライブ予定なし",
    "出演予定なし",
    "ライブ予定 アイドル道場",
    "出演予定@アイドル道場 Vol.71",
    "ライブ予定@第3回定期公演",
    "ライブ予定@渋谷(昼)",
    "ライブ予定調和",
    "出演予定者発表",
    "イベント予定地",
    "ライブ予定変更",
    "大阪ライブ予定",
    "12月ライブ予定",
    "ライブ予定(渋谷)",
    "ライブ(仮)",
    REAL,
    "『アイドル道場 Vol. 71』@渋谷VIDENT",
    "",
    None,
])
def test_real_titles_are_not_tentative(title):
    assert timetree.parse_tentative_title(title) is None


# 既存の判定は変えない。「ライブ予定@都内」を真にすると行が消えるようになる
@pytest.mark.parametrize("title, expected", [
    ("ライブ予定", True),
    ("LIVE予定", True),
    ("イベント予定", True),
    ("ライブ予定（仮）", True),
    ("ライブ予定(仮)", False),
    ("出演予定", False),
    ("主催予定", False),
    ("ライブ予定@都内", False),
    ("出演予定📍渋谷", False),
])
def test_looks_placeholder_is_unchanged(title, expected):
    assert timetree.looks_placeholder(title) is expected


# --- メモの題名 ---

@pytest.mark.parametrize("note, expected", [
    ("「アイドル道場 Vol.71」に出演します", "アイドル道場 Vol.71"),
    ("『アイドル道場 Vol.71』", "アイドル道場 Vol.71"),
    ("「アイドル道場 Vol.71」\n「アイドル道場 Vol.71」", "アイドル道場 Vol.71"),
    ("イベント名: 秋の対バン祭り", "秋の対バン祭り"),
    ("【公演名】秋の対バン祭り", "秋の対バン祭り"),
    ("タイトル：「秋の対バン祭り」", "秋の対バン祭り"),
    ("「別の引用です」\n公演名: 秋の対バン祭り", "秋の対バン祭り"),   # 見出しが最優先
    ("<p>「アイドル道場 Vol.71」</p><br>", "アイドル道場 Vol.71"),
    ("11/4 「アイドル道場 Vol.71」", "アイドル道場 Vol.71"),         # 公演日と一致
    ("「アイドル道場 Vol.71」\n一般発売 10/1 12:00", "アイドル道場 Vol.71"),  # 発売日は数えない
])
def test_note_title_picks(note, expected):
    assert timetree.note_title(note, DAY, NAME) == expected


@pytest.mark.parametrize("note", [
    None,
    "",
    "渋谷で出演します",
    "「イベントA」と「イベントB」",              # 1つに絞れない
    "物販は「チェキ券」のみ",
    "「先行チケット受付」",
    "新曲「はじまりの歌」を披露",
    "「ライブ予定」",                            # メモ側も仮タイトル
    "「ライブ予定@都内」",
    "「出演予定なし」",                          # メモ側が「無い告知」
    "「テスト」",                                # グループ名そのもの
    "11/5 「アイドル道場 Vol.71」",              # 別の日の公演
    "公演名: ライブ予定\n「アイドル道場 Vol.71」",   # 見出しが読めないなら引用へ逃げない
    "タイトル未定",
])
def test_note_title_declines(note):
    assert timetree.note_title(note, DAY, NAME) is None


# --- 会場の照合 ---

@pytest.mark.parametrize("a, b, expected", [
    ("渋谷VIDENT", "渋谷VIDENT", True),
    ("渋谷VIDENT", "VIDENT", True),
    ("渋谷VIDENT", "VIDENT(東京都渋谷区)", True),
    ("渋谷VIDENT", "Spotify O-nest", False),
    ("Spotify O-nest", "Spotify O-EAST", False),
    ("新宿ReNY", "ReNY alpha", False),
    ("渋谷CLUB QUATTRO", "名古屋CLUB QUATTRO", False),
    ("TSUTAYA O-EAST", "Spotify O-EAST", False),
    ("渋谷", "渋谷VIDENT", False),
])
def test_same_venue(a, b, expected):
    assert venuetext.same_venue(a, b) is expected


def test_same_venue_uses_aliases():
    aliases = venuetext.venue_alias_map([{"id": "rensa", "name": "仙台Rensa", "aliases": ["RENSA", "レンサ"]}])
    assert venuetext.same_venue("レンサ", "仙台Rensa", aliases)
    assert not venuetext.same_venue("レンサ", "仙台Rensa")


@pytest.mark.parametrize("place, kind", [
    (None, "none"), ("都内", "wide"), ("東京", "wide"), ("渋谷", "area"),
    ("shibuya", "area"), ("渋谷VIDENT", "venue"),
])
def test_place_kind(place, kind):
    assert venuetext.place_kind(place) == kind


# --- 段0: 対応づけ ---

def _row(tid="tt:artist:テスト:1", title="ライブ予定@渋谷VIDENT", dt=f"{DAY} 18:00", **extra):
    return {"tweet_id": tid, "idol_slug": SLUG, "event_name": title, "event_datetime": dt, **extra}


def _cand(tid="yrt:artist:テスト:x", title=REAL, dt=f"{DAY} 18:30", **extra):
    return _row(tid, title, dt, **extra)


def test_match_by_same_ticket_url():
    row = _row(title="ライブ予定", ticket_url="https://t-dv.com/abc")
    cand = _cand(ticket_url="https://ticketdive.com/event/abc")
    m = worker.match_placeholder(row, [row, cand])
    assert (m["status"], m["rule"]) == ("matched", "T")
    assert m["candidates"][0]["basis"] == "URL一致"
    assert m["candidates"][0]["gap_min"] == 30


def test_match_by_venue():
    row = _row()
    cand = _cand(title="『アイドル道場 Vol. 71』@渋谷VIDENT")
    m = worker.match_placeholder(row, [row, cand])
    assert (m["status"], m["rule"]) == ("matched", "A")
    assert m["candidates"][0]["basis"] == "会場一致"


def test_match_reads_venue_from_other_details_and_parenthesis():
    assert worker.match_placeholder(_row(), [_cand(other_details="VIDENT")])["rule"] == "A"
    row = _row(title="ライブ予定@新宿ReNY")
    assert worker.match_placeholder(row, [_cand(title="あいらんど Chapter11（新宿ReNY）")])["rule"] == "A"


@pytest.mark.parametrize("cand_dt, status", [
    (f"{DAY} 21:00", "matched"),   # ちょうど3時間
    (f"{DAY} 21:01", "skipped"),   # 3時間を超える
    (DAY, "matched"),              # 片方が時刻なしなら不問
])
def test_time_gap_boundary(cand_dt, status):
    assert worker.match_placeholder(_row(), [_cand(dt=cand_dt, other_details="渋谷VIDENT")])["status"] == status


def test_time_gap_uses_nearer_of_start_and_open():
    cand = _cand(dt=f"{DAY} 22:00", open_time="20:30", other_details="渋谷VIDENT")
    m = worker.match_placeholder(_row(), [cand])
    assert m["status"] == "matched"
    assert m["candidates"][0]["gap_min"] == 150


def test_url_match_is_skipped_when_time_gap_is_large():
    row = _row(title="ライブ予定", ticket_url="https://t-dv.com/abc")
    cand = _cand(dt=f"{DAY} 22:00", ticket_url="https://t-dv.com/abc")
    m = worker.match_placeholder(row, [cand])
    assert m["status"] == "skipped"
    assert "時刻差" in m["reason"]


@pytest.mark.parametrize("title, cand_venue", [
    ("ライブ予定", "渋谷VIDENT"),          # 場所なし
    ("ライブ予定@都内", "渋谷VIDENT"),     # 広域
    ("出演予定@渋谷", "渋谷VIDENT"),       # エリア一致のみ
    ("出演予定@渋谷", "池袋mono"),         # エリア不一致
    ("ライブ予定@渋谷VIDENT", "Spotify O-nest"),  # 同じエリアの別会場
    ("ライブ予定@渋谷VIDENT", None),       # 候補の会場が不明
])
def test_place_only_does_not_match(title, cand_venue):
    m = worker.match_placeholder(_row(title=title), [_cand(other_details=cand_venue)])
    assert m["status"] == "skipped"
    assert m["rule"] is None
    assert not any(c["hit"] for c in m["candidates"])


def test_two_candidates_at_same_venue_are_skipped():
    cands = [_cand("yrt:a", "昼公演 特別編", other_details="VIDENT"),
             _cand("site:b", "夜公演 特別編", other_details="渋谷VIDENT")]
    assert worker.match_placeholder(_row(), cands)["status"] == "skipped"


def test_ttx_is_counted_but_not_adopted():
    only_ttx = [_cand("ttx:artist:テスト:y", other_details="渋谷VIDENT")]
    assert worker.match_placeholder(_row(), only_ttx)["status"] == "skipped"
    both = only_ttx + [_cand(other_details="渋谷VIDENT")]
    assert worker.match_placeholder(_row(), both)["status"] == "skipped"


@pytest.mark.parametrize("cand", [
    _cand(tid="tt:artist:テスト:self"),                       # 自分自身
    _cand(title="ライブ予定@渋谷VIDENT"),                     # 仮タイトル
    _cand(title="ライブなし", other_details="渋谷VIDENT"),    # 告知
    _cand(title="SCHEDULE", other_details="渋谷VIDENT"),      # 見出し
    _cand(title="一般発売", other_details="渋谷VIDENT"),      # 受付
    _cand(title="ラジオ番組 特別編", other_details="渋谷VIDENT"),  # 放送
    _cand(dt="2099-11-05 18:00", other_details="渋谷VIDENT"),  # 別の日
    dict(_cand(other_details="渋谷VIDENT"), idol_slug="artist:別"),  # 別のグループ
    _cand(other_details="渋谷VIDENT", hidden=True),           # 隠した行
])
def test_rows_that_are_not_candidates(cand):
    row = _row("tt:artist:テスト:self")
    assert worker.match_placeholder(row, [cand])["status"] == "no_candidate"


def test_title_and_details_conflict_is_skipped():
    row = _row(other_details="新宿ReNY")
    m = worker.match_placeholder(row, [_cand(other_details="渋谷VIDENT")])
    assert m["status"] == "skipped"
    assert "食い違う" in m["reason"]


def test_ticket_listing_pages_are_not_the_same_page():
    row = _row(title="ライブ予定", ticket_url="https://livepocket.jp/event/search")
    cand = _cand(ticket_url="https://livepocket.jp/event/search")
    assert worker.match_placeholder(row, [cand])["status"] == "skipped"


def test_same_candidate_for_two_rows_is_skipped():
    rows = [_row("tt:artist:テスト:1"), _row("site:artist:テスト:2"), _cand(other_details="渋谷VIDENT")]
    report = worker.placeholder_shadow_report(rows)
    assert report["total"] == 2
    assert report["matched"] == 0
    assert report["skipped"] == 2


# --- 段0: 実行(DB を変更しない) ---

@pytest.fixture
def fake_sb(monkeypatch):
    fake = MagicMock()
    fake.get_last_seen.return_value = None
    fake.get_auto_event_ids.return_value = []
    monkeypatch.setattr(worker, "sb", fake)
    monkeypatch.setattr(worker, "build_idol_name_list", lambda: [])
    return fake


def _shadow_rows():
    return [
        _row("tt:artist:テスト:1", raw_text="「アイドル道場 Vol.71」"),
        _cand(other_details="渋谷VIDENT"),
        _row("tt:artist:テスト:2", title="ライブ予定@都内", dt="2099-11-05 18:00"),
        _cand("yrt:artist:テスト:z", "別の公演", "2099-11-05 18:00"),
        _row("site:artist:テスト:3", title="出演予定", dt="2099-11-06 18:00"),
        _row("ttx:artist:テスト:4", title="ライブ予定", dt="2099-11-07 18:00"),
        _row("yrt:artist:テスト:5", title="12月ライブ予定", dt="2099-11-08 18:00"),
    ]


def test_reconcile_never_writes(fake_sb, capsys):
    fake_sb.get_upcoming_rows_for_placeholder.return_value = _shadow_rows()

    report = worker.reconcile_placeholder_rows()

    assert [c[0] for c in fake_sb.method_calls] == ["get_upcoming_rows_for_placeholder"]
    assert (report["total"], report["matched"], report["skipped"], report["no_candidate"]) == (3, 1, 1, 1)
    assert report["ttx_tentative"] == 1
    assert report["note_title"] == 1
    assert report["near_miss"] == {"12月ライブ予定": 1}


def test_reconcile_log_can_be_followed_row_by_row(fake_sb, capsys):
    fake_sb.get_upcoming_rows_for_placeholder.return_value = _shadow_rows()

    worker.reconcile_placeholder_rows()

    out = capsys.readouterr().out
    assert "[対応あり: 規則A: 会場の完全一致]" in out
    assert f"{DAY} 18:00 | {SLUG} | ライブ予定@渋谷VIDENT | tt:" in out
    assert f"→ 候補: {REAL} | 会場 渋谷VIDENT | 根拠 会場一致 | 時刻差 30分" in out
    assert "同日の本物の行なし 1" in out
    # 同日の本物の行が無い行は件数だけ
    assert "tt:artist:テスト:2" in out
    assert "site:artist:テスト:3" not in out
    assert "12月ライブ予定" in out


def test_reconcile_writes_step_summary(fake_sb, monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    fake_sb.get_upcoming_rows_for_placeholder.return_value = _shadow_rows()

    worker.reconcile_placeholder_rows()

    text = path.read_text(encoding="utf-8")
    assert "仮タイトルの照合(試運転・DB変更なし)" in text
    assert "対応あり(規則A)" in text
    assert "site:artist:テスト:3" not in text


def test_guarded_reconcile_survives_failure(fake_sb, capsys):
    fake_sb.get_upcoming_rows_for_placeholder.side_effect = RuntimeError("読めない")
    worker._reconcile_placeholder_guarded()
    assert "仮タイトルの照合に失敗" in capsys.readouterr().out


# --- 第1段: 書き換え ---

def _tt_payload(*events):
    start = int(datetime.datetime(2099, 11, 4, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    return json.dumps({"public_events": [
        {"id": i, "title": t, "note": n, "start_at": start, "all_day": False}
        for i, (t, n) in enumerate(events)
    ]}, ensure_ascii=False).encode("utf-8")


def _run_tt(monkeypatch, fake_sb, *events):
    monkeypatch.setattr(worker, "build_timetree_watch_list", lambda: [
        {"idol_slug": SLUG, "name": NAME, "tt_url": "https://timetreeapp.com/public_calendars/alias"}])
    monkeypatch.setattr(worker, "_rotate", lambda watch, *a, **k: watch)
    monkeypatch.setattr(worker, "repair_lineup_days", lambda *a, **k: None)
    monkeypatch.setattr(timetree, "_get", lambda *a, **k: _tt_payload(*events))
    worker.run_timetree()
    return [c.args[0] for c in fake_sb.upsert_auto_event.call_args_list]


def _run_gc(monkeypatch, fake_sb, *events):
    monkeypatch.setattr(worker, "build_gcal_watch_list", lambda: [
        {"idol_slug": SLUG, "name": NAME,
         "gc_url": "https://calendar.google.com/calendar/embed?src=cal@example.com"}])
    body = "".join(
        f"BEGIN:VEVENT\r\nUID:{i}\r\nSUMMARY:{t}\r\nDESCRIPTION:{n}\r\n"
        "DTSTART:20991104T180000\r\nEND:VEVENT\r\n"
        for i, (t, n) in enumerate(events))
    monkeypatch.setattr(gcal, "_get", lambda *a, **k: f"BEGIN:VCALENDAR\r\n{body}END:VCALENDAR\r\n")
    worker.run_gcal()
    return [r for c in fake_sb.upsert_auto_events.call_args_list for r in c.args[0]]


@pytest.mark.parametrize("title", ["ライブ予定@都内", "ライブ予定", "出演予定", "ライブ予定(仮)@都内"])
def test_timetree_rewrites_from_note(monkeypatch, fake_sb, capsys, title):
    rows = _run_tt(monkeypatch, fake_sb, (title, f"「{REAL}」"))
    assert [r["event_name"] for r in rows] == [REAL]
    out = capsys.readouterr().out
    assert f"→ {REAL}(根拠: メモ)" in out


def test_gcal_rewrites_from_note(monkeypatch, fake_sb, capsys):
    rows = _run_gc(monkeypatch, fake_sb, ("ライブ予定@都内", f"「{REAL}」"))
    assert [r["event_name"] for r in rows] == [REAL]
    assert f"ライブ予定@都内 → {REAL}" in capsys.readouterr().out


def test_rewrite_moves_title_place_to_empty_venue(monkeypatch, fake_sb):
    rows = _run_tt(monkeypatch, fake_sb, ("ライブ予定@渋谷VIDENT", f"「{REAL}」"))
    assert rows[0]["event_name"] == REAL
    assert rows[0]["other_details"] == "渋谷VIDENT"


@pytest.mark.parametrize("title, note", [
    ("ライブ予定@都内", ""),                       # メモなし
    ("ライブ予定@都内", "渋谷で出演します"),        # 引用なし
    ("ライブ予定@都内", "「ライブ予定」"),          # メモも仮タイトル
    ("ライブ予定@都内", "「出演予定なし」"),        # メモが「無い告知」
    ("ライブ予定@都内", "「イベントA」「イベントB」"),  # 絞れない
    ("ライブ予定@都内", "物販は「チェキ券」のみ"),   # 公演名ではない引用
    ("ライブ予定@都内", "11/5 「別の日の公演」"),   # 日付が公演日と違う
    ("ライブ予定@都内", f"「{NAME}」"),             # グループ名そのもの
])
def test_timetree_keeps_title_without_real_name(monkeypatch, fake_sb, title, note):
    rows = _run_tt(monkeypatch, fake_sb, (title, note))
    assert [r["event_name"] for r in rows] == [title]


def test_gcal_keeps_title_when_note_is_also_tentative(monkeypatch, fake_sb):
    rows = _run_gc(monkeypatch, fake_sb, ("ライブ予定@都内", "「ライブ予定」"))
    assert [r["event_name"] for r in rows] == ["ライブ予定@都内"]


def test_real_title_is_never_rewritten(monkeypatch, fake_sb):
    rows = _run_tt(monkeypatch, fake_sb, ("定期公演 Vol.10", "「別のイベント名」"))
    assert [r["event_name"] for r in rows] == ["定期公演 Vol.10"]


def test_tentative_rows_without_ticket_are_not_dropped(monkeypatch, fake_sb):
    rows = _run_tt(monkeypatch, fake_sb, ("ライブ予定@渋谷", ""))
    assert [r["event_name"] for r in rows] == ["ライブ予定@渋谷"]
    assert _run_gc(monkeypatch, fake_sb, ("ライブ予定@渋谷", "メモ"))[0]["event_name"] == "ライブ予定@渋谷"


def test_bare_placeholder_without_any_clue_is_still_dropped(monkeypatch, fake_sb):
    assert _run_tt(monkeypatch, fake_sb, ("ライブ予定", "")) == []


def test_bare_placeholder_with_ambiguous_quotes_is_kept_with_original_title(monkeypatch, fake_sb):
    rows = _run_tt(monkeypatch, fake_sb, ("ライブ予定", "「イベントA」「イベントB」"))
    assert [r["event_name"] for r in rows] == ["ライブ予定"]


def test_declined_quote_is_logged(monkeypatch, fake_sb, capsys):
    _run_tt(monkeypatch, fake_sb, ("ライブ予定", "物販は「チェキ券」のみ"))
    assert "メモの引用を題名にせず: ライブ予定 / 引用「チェキ券」" in capsys.readouterr().out


def test_retitles_go_to_step_summary(monkeypatch, fake_sb, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    _run_tt(monkeypatch, fake_sb, ("ライブ予定@都内", f"「{REAL}」"))
    text = path.read_text(encoding="utf-8")
    assert f"| ライブ予定@都内 | {REAL} | メモ |" in text


# --- 第1段: 自分のチケットページ ---

TD_URL = "https://t-dv.com/abc"


def _td(monkeypatch, date=DAY, venue="渋谷VIDENT", name="公式の公演名"):
    info = {"event_name": name, "venue": venue, "event_datetime": f"{date} 18:00:00",
            "day_stages": [{"date": date, "venue": venue}]}
    monkeypatch.setattr(worker, "_td_info", lambda url: info)


def _td_row(**extra):
    return {"tweet_id": "tt:x:1", "idol_slug": SLUG, "event_name": "ライブ予定@都内",
            "event_datetime": f"{DAY} 18:00", "ticket_url": TD_URL, "raw_text": None, **extra}


def test_ticketdive_name_is_used_when_date_matches(monkeypatch):
    _td(monkeypatch)
    row = _td_row()
    assert worker.resolve_tentative_title(row, NAME) == "TicketDive(公演日一致)"
    assert row["event_name"] == "公式の公演名"


def test_ticketdive_takes_priority_over_note(monkeypatch):
    _td(monkeypatch)
    row = _td_row(raw_text=f"「{REAL}」")
    worker.resolve_tentative_title(row, NAME)
    assert row["event_name"] == "公式の公演名"


@pytest.mark.parametrize("kwargs, extra", [
    ({"date": "2099-11-05"}, {}),                        # 公演日が違う
    ({}, {"other_details": "新宿ReNY"}),                 # 会場が食い違う
    ({}, {"event_name": "ライブ予定@新宿ReNY"}),         # タイトルの会場が食い違う
    ({"name": "ライブ予定"}, {}),                        # ページの名前も仮タイトル
])
def test_ticketdive_name_is_not_used(monkeypatch, kwargs, extra):
    _td(monkeypatch, **kwargs)
    row = _td_row(**extra)
    before = row["event_name"]
    assert worker.resolve_tentative_title(row, NAME) is None
    assert row["event_name"] == before


def test_ticketdive_fetch_failure_falls_back_to_note(monkeypatch):
    monkeypatch.setattr(worker, "_td_info", lambda url: None)
    row = _td_row(raw_text=f"「{REAL}」")
    assert worker.resolve_tentative_title(row, NAME) == "メモ"
    assert row["event_name"] == REAL


def test_ticketdive_is_not_read_when_use_ticket_is_off(monkeypatch):
    _td(monkeypatch)
    row = _td_row()
    assert worker.resolve_tentative_title(row, NAME, use_ticket=False) is None
    assert row["event_name"] == "ライブ予定@都内"
