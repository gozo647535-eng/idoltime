"""カレンダー由来の「出演なし」「ライブなし」を行にしない・既存行を消す処理のテスト。

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
import worker  # noqa: E402

SLUG = "artist:テスト"
NAME = "テスト"
REAL_TITLE = "定期公演 Vol.10"


# --- 判定関数 ---

@pytest.mark.parametrize("title", [
    "出演なし",
    "ライブなし",
    "ライブ予定なし",
    "出演予定なし",
    "予定なし",
    "イベントなし",
    "公演なし",
    "配信なし",
    "特典会なし",
    "本日ライブなし",
    "本日はライブなし",
    "今日はライブなし",
    "本日はなし",
    "ライブはなし",
    "ライブの予定なし",
    "ライブの予定はありません",
    "ライブはありません",
    "本日ライブはありません",
    "予定はありません",
    # 正規化: 全角半角・空白・句読点・前後の飾り
    "ﾗｲﾌﾞなし",
    "ライブ なし",
    "ライブ　なし。",
    "出演なし!",
    "【ライブなし】",
    "🙅ライブなし🙅",
])
def test_no_event_titles_match(title):
    assert timetree.is_no_event_title(title)


@pytest.mark.parametrize("title", [
    "涙なしには語れない夜",
    "なしくずしライブ",
    "ライブなしでは生きられない",
    "はなし",              # 「話」と読める
    "ははなし",
    "はなしの会",
    "ライブなし祭り",
    "出演なし(体調不良のため)",   # 全体が告知とは言い切れないので保守的に残す
    "11/4 ライブなし",
    "ライブ",
    "定期公演",
    REAL_TITLE,
    "ライブ予定",           # プレースホルダは別の判定に任せる
    "",
])
def test_real_titles_do_not_match(title):
    assert not timetree.is_no_event_title(title)


def test_none_title_does_not_match():
    assert not timetree.is_no_event_title(None)


# --- 取り込み経路 ---

def _ics(*events):
    """(UID, SUMMARY, DTSTART) の並びから ICS を作る。"""
    body = "".join(
        f"BEGIN:VEVENT\r\nUID:{u}\r\nSUMMARY:{s}\r\nDTSTART;VALUE=DATE:{d}\r\nEND:VEVENT\r\n"
        for u, s, d in events)
    return f"BEGIN:VCALENDAR\r\n{body}END:VCALENDAR\r\n"


def _tt_payload(*titles):
    start = int(datetime.datetime(2099, 11, 4, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    return json.dumps({"public_events": [
        {"id": i, "title": t, "start_at": start, "all_day": False}
        for i, t in enumerate(titles)
    ]}, ensure_ascii=False).encode("utf-8")


def test_gcal_fetch_skips_no_event(monkeypatch):
    monkeypatch.setattr(gcal, "_get", lambda *a, **k: _ics(
        ("a", "出演なし", "20991104"),
        ("b", "[Live]ライブなし", "20991105"),
        ("c", REAL_TITLE, "20991106"),
        ("d", "出演なし", "20000101"),   # 過去分はそもそも対象外なので数えない
    ))
    skipped = []
    content_hash, events = gcal.fetch_public_events("cal@example.com", skipped=skipped)
    assert content_hash
    assert [e["title"] for e in events] == [REAL_TITLE]
    assert skipped == ["出演なし", "[Live]ライブなし"]


def test_timetree_fetch_skips_no_event(monkeypatch):
    monkeypatch.setattr(timetree, "_get", lambda *a, **k: _tt_payload("ライブなし", REAL_TITLE))
    skipped = []
    content_hash, events = timetree.fetch_public_events("alias", skipped=skipped)
    assert content_hash
    assert [e["title"] for e in events] == [REAL_TITLE]
    assert skipped == ["ライブなし"]


def test_fetch_works_without_skipped_argument(monkeypatch):
    monkeypatch.setattr(timetree, "_get", lambda *a, **k: _tt_payload("ライブなし"))
    assert timetree.fetch_public_events("alias")[1] == []


@pytest.fixture
def fake_sb(monkeypatch):
    fake = MagicMock()
    fake.get_last_seen.return_value = None
    fake.get_auto_event_ids.return_value = []
    monkeypatch.setattr(worker, "sb", fake)
    monkeypatch.setattr(worker, "build_idol_name_list", lambda: [])
    return fake


def test_run_gcal_writes_no_row_and_reconciles(monkeypatch, fake_sb, capsys):
    stale = f"gc:{SLUG}:old"
    fake_sb.get_auto_event_ids.return_value = [stale]
    monkeypatch.setattr(worker, "build_gcal_watch_list", lambda: [
        {"idol_slug": SLUG, "name": NAME,
         "gc_url": "https://calendar.google.com/calendar/embed?src=cal@example.com"}])
    monkeypatch.setattr(gcal, "_get", lambda *a, **k: _ics(
        ("a", "出演なし", "20991104"), ("b", "出演なし", "20991105"), ("c", REAL_TITLE, "20991106")))

    assert worker.run_gcal() == 1

    written = [r for c in fake_sb.upsert_auto_events.call_args_list for r in c.args[0]]
    assert [r["event_name"] for r in written] == [REAL_TITLE]
    fake_sb.delete_auto_event.assert_called_once_with(stale)
    out = capsys.readouterr().out
    assert "公演なしの告知を除外: 2 件" in out
    assert "「出演なし」×2" in out


def test_run_timetree_writes_no_row(monkeypatch, fake_sb, capsys):
    monkeypatch.setattr(worker, "build_timetree_watch_list", lambda: [
        {"idol_slug": SLUG, "name": NAME,
         "tt_url": "https://timetreeapp.com/public_calendars/alias"}])
    monkeypatch.setattr(worker, "_rotate", lambda watch, *a, **k: watch)
    monkeypatch.setattr(worker, "repair_lineup_days", lambda *a, **k: None)
    monkeypatch.setattr(timetree, "_get", lambda *a, **k: _tt_payload("ライブなし", REAL_TITLE))

    assert worker.run_timetree() == 1

    written = [c.args[0] for c in fake_sb.upsert_auto_event.call_args_list]
    assert [r["event_name"] for r in written] == [REAL_TITLE]
    out = capsys.readouterr().out
    assert "公演なしの告知を除外: 1 件" in out
    assert "「ライブなし」×1" in out


def test_skips_are_logged_even_when_calendar_is_unchanged(monkeypatch, fake_sb, capsys):
    monkeypatch.setattr(worker, "build_gcal_watch_list", lambda: [
        {"idol_slug": SLUG, "name": NAME,
         "gc_url": "https://calendar.google.com/calendar/embed?src=cal@example.com"}])
    monkeypatch.setattr(gcal, "_get", lambda *a, **k: _ics(("a", "出演なし", "20991104")))
    fake_sb.get_last_seen.side_effect = lambda key: gcal.fetch_public_events("cal@example.com")[0]

    assert worker.run_gcal() == 0

    fake_sb.upsert_auto_events.assert_not_called()
    assert "公演なしの告知を除外: 1 件" in capsys.readouterr().out


def test_skips_go_to_step_summary(monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    worker._report_no_event_skips("TimeTree", [("アブソプ", "ライブなし"), ("アブソプ", "ライブなし")])
    text = path.read_text(encoding="utf-8")
    assert "公演なしの告知を除外 2 件" in text
    assert "アブソプ: 「ライブなし」×2" in text


# --- 既存行の掃除 ---

def _rows_by_prefix(prefix):
    return {
        "tt:": [
            {"tweet_id": "tt:artist:アブソプ:1", "idol_slug": "artist:アブソプ", "event_name": "ライブなし"},
            {"tweet_id": "tt:artist:アブソプ:2", "idol_slug": "artist:アブソプ", "event_name": "涙なしには語れない夜"},
            {"tweet_id": "tt:artist:アブソプ:3", "idol_slug": "artist:アブソプ", "event_name": None},
            # 接頭辞が違う行は、取得側から紛れても消さない
            {"tweet_id": "ttx:artist:他:4", "idol_slug": "artist:他", "event_name": "ライブなし"},
        ],
        "gc:": [
            {"tweet_id": "gc:artist:シンセカイヒーロー:1", "idol_slug": "artist:シンセカイヒーロー", "event_name": "出演なし"},
            {"tweet_id": "gc:artist:シンセカイヒーロー:2", "idol_slug": "artist:シンセカイヒーロー", "event_name": REAL_TITLE},
        ],
    }[prefix]


def test_cleanup_deletes_only_exact_matches_in_calendar_prefixes(fake_sb, capsys):
    fake_sb.get_auto_event_rows_by_prefix.side_effect = _rows_by_prefix

    assert worker.cleanup_no_event_rows() == 2

    fake_sb.delete_auto_events.assert_called_once_with(
        ["tt:artist:アブソプ:1", "gc:artist:シンセカイヒーロー:1"])
    assert [c.args[0] for c in fake_sb.get_auto_event_rows_by_prefix.call_args_list] == ["tt:", "gc:"]
    out = capsys.readouterr().out
    assert "2 件削除" in out
    assert "「ライブなし」×1" in out


def test_cleanup_does_nothing_without_matches(fake_sb):
    fake_sb.get_auto_event_rows_by_prefix.return_value = [
        {"tweet_id": "tt:a:1", "idol_slug": "a", "event_name": REAL_TITLE}]
    assert worker.cleanup_no_event_rows() == 0
    fake_sb.delete_auto_events.assert_not_called()


def test_cleanup_read_failure_keeps_other_prefix(fake_sb):
    def rows(prefix):
        if prefix == "tt:":
            raise RuntimeError("読めない")
        return _rows_by_prefix(prefix)
    fake_sb.get_auto_event_rows_by_prefix.side_effect = rows

    assert worker.cleanup_no_event_rows() == 1
    fake_sb.delete_auto_events.assert_called_once_with(["gc:artist:シンセカイヒーロー:1"])


def test_cleanup_delete_failure_is_not_fatal(fake_sb, capsys):
    fake_sb.get_auto_event_rows_by_prefix.side_effect = _rows_by_prefix
    fake_sb.delete_auto_events.side_effect = RuntimeError("消せない")

    assert worker.cleanup_no_event_rows() == 0
    assert "削除に失敗" in capsys.readouterr().out


def test_orphan_cleanup_runs_sweep_even_when_it_aborts(monkeypatch, fake_sb):
    """孤児の判定が監視対象の取得失敗で中止になっても、告知の掃除は走る。"""
    fake_sb.get_auto_event_rows_by_prefix.side_effect = _rows_by_prefix

    def boom():
        raise RuntimeError("図鑑を読めない")
    monkeypatch.setattr(worker, "build_timetree_watch_list", boom)

    assert worker.cleanup_orphan_rows() == 0
    fake_sb.delete_auto_events.assert_called_once_with(
        ["tt:artist:アブソプ:1", "gc:artist:シンセカイヒーロー:1"])
