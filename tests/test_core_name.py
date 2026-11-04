"""タイトル整形で半角の括弧が落ちる不具合のテスト。

ネットワークには触れない。取得関数はすべて差し替える。
"""
import datetime
import json

import pytest

import gcal
import timetree


# --- 表示用の整形 ---

@pytest.mark.parametrize("title", [
    "定期公演(昼)",
    "ライブ(仮)",
    "定期公演（夜）",
    "ライブ（仮）",
    "定期公演(昼)夜の部",
    "(仮)定期公演",
    "（仮）定期公演",
    "(昼公演)",
    "定期公演 Vol.10",
])
def test_parentheses_are_kept(title):
    assert timetree._core_name(title) == title


@pytest.mark.parametrize("title, expected", [
    ("🎤定期公演🎤", "定期公演"),
    ("🎤定期公演(昼)🎤", "定期公演(昼)"),
    ("★(仮)定期公演★", "(仮)定期公演"),
    ("♪定期公演（夜）♪", "定期公演（夜）"),
    ("♪【生誕祭】♪", "【生誕祭】"),
    ("定期公演!!🎉", "定期公演!!"),
    ("定期公演(", "定期公演"),      # 閉じていない括弧は飾りのまま落とす
    ("🎉🎉", ""),
    ("", ""),
    (None, ""),
])
def test_surrounding_decorations_are_dropped(title, expected):
    assert timetree._core_name(title) == expected


# --- 判定は表示用の括弧の扱いに左右されない ---

@pytest.mark.parametrize("title", [
    "定期公演(昼)",
    "ライブ(仮)",      # 表示用の芯で判定すると True になり、行が消えてしまう
    "(昼公演)",
    "(仮)ライブ",
])
def test_halfwidth_parentheses_do_not_make_placeholder(title):
    assert not timetree.looks_placeholder(title)
    assert not timetree.looks_placeholder(timetree._core_name(title))  # e+ は整形後を渡す


@pytest.mark.parametrize("title", [
    "ライブ予定",
    "🎤ライブ予定🎤",
    "大阪遠征",
    "(未定)",
    "ライブ（仮）",
    "（出演日未定）",
    "",
])
def test_placeholders_still_match(title):
    assert timetree.looks_placeholder(title)
    assert timetree.looks_placeholder(timetree._core_name(title))


@pytest.mark.parametrize("title", [
    "(ライブなし)",
    "（ライブなし）",
    "(🙅ライブなし🙅)",   # 括弧の内側の飾りも従来どおり落ちる
    "（★ライブなし★）",
    "ライブなし(🙅)",
])
def test_no_event_titles_in_parentheses_still_match(title):
    assert timetree.is_no_event_title(title)


def test_no_event_title_with_real_reason_still_kept():
    assert not timetree.is_no_event_title("出演なし(体調不良のため)")


# --- 取得経路 ---

def test_timetree_fetch_keeps_closing_parenthesis(monkeypatch):
    start = int(datetime.datetime(2099, 11, 4, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    payload = json.dumps({"public_events": [
        {"id": i, "title": t, "start_at": start, "all_day": False}
        for i, t in enumerate(["定期公演(昼)", "ライブ(仮)"])
    ]}, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(timetree, "_get", lambda *a, **k: payload)

    _, events = timetree.fetch_public_events("alias")

    assert [e["title"] for e in events] == ["定期公演(昼)", "ライブ(仮)"]


def test_gcal_fetch_keeps_closing_parenthesis(monkeypatch):
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nSUMMARY:[Live]定期公演(昼)\r\n"
        "DTSTART;VALUE=DATE:20991104\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    monkeypatch.setattr(gcal, "_get", lambda *a, **k: ics)

    _, events = gcal.fetch_public_events("cal@example.com")

    assert [e["title"] for e in events] == ["定期公演(昼)"]
