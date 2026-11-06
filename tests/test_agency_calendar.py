"""事務所の共有カレンダー(ペチュニア)で、他グループの予定を持ち主の行にしない処理のテスト。

設計: AGENCY-CALENDAR-DESIGN-2026-11-06.md(案B)。
ネットワークにも Supabase にも触れない。sb と取得関数はすべて差し替える。
"""
import datetime
import json
import os
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# worker / sb は import 時に環境変数を読むので、実環境の設定を持ち込まない
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "YRT_FORCE"):
    os.environ.pop(_key, None)

import timetree  # noqa: E402
import worker  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "timetree" / "pechunia-public-events.json"
OWNER = "ギミラブ！"
SLUG = "artist:ギミラブ！"
ALIAS = "pechunia"
TT_URL = f"https://timetreeapp.com/public_calendars/{ALIAS}"

GIMILOVE = "ギミラブ"
BUNSUIREI = "文翠麗"

# §4.3 の表: (#, イベントID, 期待の判定, 期待の理由, 所属タグ)
TABLE = [
    (1, "3078622539552681707", "drop", f"other-tag:{BUNSUIREI}", {BUNSUIREI}),   # 11/7 渋谷LIVE
    (2, "3093092378274983508", "keep", "owner-mentioned", {GIMILOVE}),          # 11/7 ペチュニアLIVE
    (3, "3093098056506961465", "keep", "owner-mentioned", {GIMILOVE}),          # 11/7 IDOL TIME 仙台(対バン)
    (4, "3047439316399451162", "keep", "owner-mentioned", set()),               # 11/8 齋藤きらら 卒業LIVE
    (5, "3086483339608878658", "keep", "owner-mentioned", {GIMILOVE}),          # 11/8 仙台LIVE(対バン)
    (6, "3083666034101349710", "drop", f"other-tag:{BUNSUIREI}", {BUNSUIREI}),   # 11/14 秋葉原LIVE
    (7, "3082242619318496498", "drop", f"other-tag:{BUNSUIREI}", {BUNSUIREI}),   # 11/16 渋谷LIVE
    (8, "3083663440108875708", "drop", f"other-tag:{BUNSUIREI}", {BUNSUIREI}),   # 11/19 秋葉原LIVE
    (9, "3047439653680213915", "keep", "owner-mentioned", {GIMILOVE}),          # 11/23 生誕LIVE(全角＃)
    (10, "3047425062124416770", "keep", "owner-mentioned", {GIMILOVE}),         # 11/28 仙台LIVE
    (11, "3077694250470370327", "keep", "owner-mentioned", set()),              # 11/29 仙台LIVE
    (12, "3092306395283615134", "drop", f"other-tag:{BUNSUIREI}", {BUNSUIREI}),  # 12/5 恵比寿LIVE
    (13, "3084387370352274772", "drop", f"other-tag:{BUNSUIREI}", {BUNSUIREI}),  # 1/10 生誕祭
    (14, "3084387612212620382", "drop", f"other-tag:{BUNSUIREI}", {BUNSUIREI}),  # 1/11 生誕祭
]
KEEP_IDS = [eid for _, eid, kind, _, _ in TABLE if kind == "keep"]
DROP_IDS = [eid for _, eid, kind, _, _ in TABLE if kind == "drop"]
# DB に保存済みの誤行(12/5 は未取り込み)
STORED_DROP_IDS = [eid for eid in DROP_IDS if eid != "3092306395283615134"]


def _raw_events():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["public_events"]


@pytest.fixture(scope="module")
def raw_by_id():
    return {ev["id"]: ev for ev in _raw_events()}


def _ev(title, note):
    return {"title": title, "note": note}


# --- 9.1 純関数 ---

def test_fixture_has_14_events(raw_by_id):
    assert len(raw_by_id) == 14
    assert set(raw_by_id) == {eid for _, eid, _, _, _ in TABLE}


@pytest.mark.parametrize("no, eid, kind, reason, tags", TABLE)
def test_affiliation_tags_from_fixture(raw_by_id, no, eid, kind, reason, tags):
    assert timetree.affiliation_tags(raw_by_id[eid]["note"]) == tags


def test_fullwidth_hash_and_undated_tag():
    # 11/23 の全角「＃」は拾い、1/10 の日付なしの #一ノ瀬みやび は拾わない
    assert timetree.affiliation_tags("＃ギミラブ2026年11月\n本文") == {GIMILOVE}
    assert timetree.affiliation_tags("#文翠麗2027年1月 \n本文\n#一ノ瀬みやび") == {BUNSUIREI}
    assert timetree.affiliation_tags("#文翠麗 だけ") == set()
    assert timetree.affiliation_tags(None) == set()


def test_pechunia_is_shared():
    verdict = timetree.classify_calendar(_raw_events(), OWNER)
    assert verdict.shared
    assert verdict.tags == {GIMILOVE, BUNSUIREI}
    assert verdict.others == {BUNSUIREI}


@pytest.mark.parametrize("no, eid, kind, reason, tags", TABLE)
def test_pechunia_14_events(raw_by_id, no, eid, kind, reason, tags):
    verdict = timetree.classify_calendar(_raw_events(), OWNER)
    assert timetree.classify_event(raw_by_id[eid], OWNER, None, verdict.shared) == (kind, reason)


def test_pechunia_totals():
    events = _raw_events()
    verdict = timetree.classify_calendar(events, OWNER)
    kinds = [timetree.classify_event(ev, OWNER, None, verdict)[0] for ev in events]
    assert (kinds.count("keep"), kinds.count("drop"), kinds.count("unclear")) == (7, 7, 0)


def test_owner_mention_without_tag_keeps(raw_by_id):
    # 4番・11番: 所属タグは無いが本文に「ギミラブ！」がある
    for eid in ("3047439316399451162", "3077694250470370327"):
        ev = raw_by_id[eid]
        assert timetree.affiliation_tags(ev["note"]) == set()
        assert timetree.mentions_owner(ev["title"], ev["note"], OWNER) is True
        assert timetree.classify_event(ev, OWNER, None, True) == ("keep", "owner-mentioned")


def test_taiban_with_both_tags_keeps():
    ev = _ev("合同ライブ", "#文翠麗2026年11月\n#ギミラブ2026年11月\n出演: 文翠麗 / ギミラブ！")
    assert timetree.classify_event(ev, OWNER, None, True) == ("keep", "owner-mentioned")


def test_no_signal_on_shared_is_unclear(raw_by_id):
    ev = dict(raw_by_id["3077694250470370327"])
    ev["note"] = ev["note"].replace("ギミラブ！", "")
    assert timetree.mentions_owner(ev["title"], ev["note"], OWNER) is False
    assert timetree.classify_event(ev, OWNER, None, True) == ("unclear", "no-signal")
    # 共有でなければ通常どおり残す
    assert timetree.classify_event(ev, OWNER, None, False) == ("keep", "ordinary")


def test_only_owner_events_not_shared(raw_by_id):
    owner_events = [raw_by_id[eid] for eid in KEEP_IDS]
    verdict = timetree.classify_calendar(owner_events, OWNER)
    assert not verdict.shared
    assert verdict.tags == {GIMILOVE}
    assert verdict.others == set()


def test_abbreviation_tag_single_calendar_untouched():
    owner = "SUPER☆GiRLS"
    events = [_ev(f"定期公演 {i}", f"#スパガ2026年11月\n公演 {i}") for i in range(3)]
    verdict = timetree.classify_calendar(events, owner)
    assert not verdict.shared
    assert [timetree.classify_event(ev, owner, None, verdict) for ev in events] == \
        [("keep", "other-tag-unshared")] * 3


def test_two_tags_without_owner_not_shared():
    owner = "SUPER☆GiRLS"
    events = [_ev("A", "#文翠麗2026年11月"), _ev("B", "#スパガ2026年11月")]
    verdict = timetree.classify_calendar(events, owner)
    assert not verdict.shared
    assert verdict.unmatched_multi
    assert all(timetree.classify_event(ev, owner, None, verdict)[0] == "keep" for ev in events)


def test_short_owner_name_noop():
    owner = "B2!"  # 正規化すると「b2」(2文字)
    ev = _ev("渋谷LIVE", "#文翠麗2026年11月")
    assert timetree.mentions_owner(ev["title"], ev["note"], owner) is None
    assert timetree.classify_event(ev, owner, None, True) == ("keep", "owner-name-too-short")


def test_alias_counts_as_mention():
    ev = _ev("渋谷LIVE", "#文翠麗2026年11月\n出演: ギミラブ")
    assert timetree.classify_event(ev, "ぎみらぶ公式ちゃん", ["ギミラブ"], True) == ("keep", "owner-mentioned")


def test_candidate_tag_counts_as_owner():
    assert timetree.same_group("ギミラブ候補生", OWNER)
    assert timetree.same_group(OWNER, "ギミラブ！💖")
    assert not timetree.same_group(BUNSUIREI, OWNER)
    events = [_ev("A", "#ギミラブ2026年11月"), _ev("B", "#ギミラブ候補生2026年11月")]
    verdict = timetree.classify_calendar(events, OWNER)
    assert verdict.others == set()
    assert timetree.classify_event(events[1], OWNER, None, verdict)[0] == "keep"


def test_mention_inside_brackets_is_kept():
    # _cal_core の括弧落としで言及が消えても、言及ありとして残す
    ev = _ev("渋谷LIVE", "#文翠麗2026年11月\n出演(ギミラブ！)")
    assert timetree.classify_event(ev, OWNER, None, True) == ("keep", "owner-mentioned")


# --- 9.2 取得 ---

class _FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=timetree.JST)
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)


def _fixed_datetime_module():
    fake = types.ModuleType("datetime")
    fake.__dict__.update({k: v for k, v in datetime.__dict__.items() if not k.startswith("__")})
    fake.datetime = _FixedDateTime
    return fake


@pytest.fixture
def pechunia_api(monkeypatch):
    data = FIXTURE.read_bytes()
    monkeypatch.setattr(timetree, "_get", lambda *a, **k: data)
    monkeypatch.setattr(timetree, "datetime", _fixed_datetime_module())


def test_fetch_returns_label(pechunia_api):
    content_hash, events = timetree.fetch_public_events(ALIAS)
    assert content_hash
    assert len(events) == 14
    labels = {e["id"]: e["label"] for e in events}
    assert {labels[eid] for eid in DROP_IDS} == {"文翠麗🪻"}
    assert {labels[eid] for eid in KEEP_IDS} == {"ギミラブ！💖"}


def test_fetch_label_missing_is_none(monkeypatch):
    start = int(datetime.datetime(2099, 11, 4, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    payload = json.dumps({"public_events": [
        {"id": 1, "title": "定期公演", "start_at": start, "all_day": False}]}).encode("utf-8")
    monkeypatch.setattr(timetree, "_get", lambda *a, **k: payload)
    _, events = timetree.fetch_public_events("alias")
    assert events[0]["label"] is None


# --- 9.3 run_timetree の結合 ---

@pytest.fixture
def fake_sb(monkeypatch):
    fake = MagicMock()
    fake.get_last_seen.return_value = None
    fake.get_auto_event_ids.return_value = []
    fake.get_future_text_rows.return_value = []
    fake.get_auto_event_ids_like.return_value = []
    monkeypatch.setattr(worker, "sb", fake)
    monkeypatch.setattr(worker, "build_idol_name_list", lambda: [])
    return fake


def test_run_timetree_drops_other_group(monkeypatch, fake_sb, pechunia_api, capsys):
    stored = [f"tt:{SLUG}:{eid}" for eid in STORED_DROP_IDS]
    fake_sb.get_auto_event_ids.return_value = stored
    monkeypatch.setattr(worker, "build_timetree_watch_list", lambda: [
        {"idol_slug": SLUG, "name": OWNER, "tt_url": TT_URL}])
    monkeypatch.setattr(worker, "_rotate", lambda watch, *a, **k: watch)
    monkeypatch.setattr(worker, "repair_lineup_days", lambda *a, **k: None)
    monkeypatch.setattr(worker, "resolve_tentative_title", lambda *a, **k: None)
    monkeypatch.setattr(worker, "enrich_row_with_ticketdive", lambda r: r)
    monkeypatch.setattr(worker, "_td_info", lambda url: None)
    seen_notes = []

    def fake_ticket(client, title, note, today):
        seen_notes.append(note)
        return None, False
    monkeypatch.setattr(timetree, "extract_note_ticket", fake_ticket)

    assert worker.run_timetree() == 7

    written = [c.args[0]["tweet_id"] for c in fake_sb.upsert_auto_event.call_args_list]
    assert sorted(written) == sorted(f"tt:{SLUG}:{eid}" for eid in KEEP_IDS)
    deleted = [c.args[0] for c in fake_sb.delete_auto_event.call_args_list]
    assert sorted(deleted) == sorted(stored)
    # drop の7件はチケット抽出(Gemini フォールバック含む)に回さない
    assert len(seen_notes) == 7
    assert not any("#文翠麗" in n for n in seen_notes)
    out = capsys.readouterr().out
    assert "共有カレンダーの他グループ予定を除外: 7 件" in out
    assert "文翠麗 ×7" in out
    assert "label: 文翠麗🪻 ×7" in out


def test_agency_report_goes_to_step_summary(monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    worker._report_agency_drops("TimeTree", {
        "dropped": [(OWNER, ALIAS, f"other-tag:{BUNSUIREI}", "文翠麗🪻", "渋谷LIVE")] * 2,
        "unclear": [],
        "trial": [("スパガ", "spg", ["スパガ"], "定期公演")],
    })
    text = path.read_text(encoding="utf-8")
    assert "共有カレンダーの他グループ予定を除外: 2 件" in text
    assert f"- {OWNER}（{ALIAS}）: 文翠麗 ×2（label: 文翠麗🪻 ×2）" in text
    assert "共有カレンダー上の判定不能（残しました）: 0 件" in text
    assert "試運転: 共有と判定しなかったカレンダーの他グループタグ: 1 件" in text
    assert "- スパガ（spg）: タグ スパガ ×1 例「定期公演」" in text


def test_agency_report_silent_when_empty(monkeypatch, tmp_path, capsys):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    worker._report_agency_drops("TimeTree", {"dropped": [], "unclear": [], "trial": []})
    assert not path.exists()
    assert capsys.readouterr().out == ""


# --- 9.4 保存済み行の掃除 ---

OTHER_SLUG = "artist:通常グループ"


def _stored_rows():
    rows = []
    for ev in _raw_events():
        rows.append({
            "tweet_id": f"tt:{SLUG}:{ev['id']}",
            "idol_slug": SLUG,
            "event_name": timetree._core_name(ev["title"]),
            "raw_text": ev["note"],
        })
    # 無関係な通常グループ(他グループのタグがあっても共有ではない)
    rows.append({"tweet_id": f"tt:{OTHER_SLUG}:1", "idol_slug": OTHER_SLUG,
                 "event_name": "定期公演", "raw_text": "#文翠麗2026年11月\n対バン"})
    rows.append({"tweet_id": f"tt:{OTHER_SLUG}:2", "idol_slug": OTHER_SLUG,
                 "event_name": "定期公演", "raw_text": "本文"})
    return rows


@pytest.fixture
def cleanup_env(monkeypatch, fake_sb):
    monkeypatch.setattr(worker, "build_idol_name_list", lambda: [
        {"idol_slug": SLUG, "name": OWNER},
        {"idol_slug": OTHER_SLUG, "name": "通常グループ"},
    ])
    fake_sb.get_future_text_rows.return_value = _stored_rows()
    derived = f"ttx:artist:POEM:{SLUG}:{DROP_IDS[0]}:20261107"

    def like(pattern):
        return [derived] if pattern == f"ttx:*:{SLUG}:{DROP_IDS[0]}:*" else []
    fake_sb.get_auto_event_ids_like.side_effect = like
    return fake_sb, derived


def test_cleanup_agency_rows_deletes_other_group(cleanup_env, capsys):
    fake, derived = cleanup_env

    assert worker.cleanup_agency_rows() == 7

    fake.delete_auto_events.assert_called_once()
    deleted = fake.delete_auto_events.call_args.args[0]
    assert sorted(deleted) == sorted([f"tt:{SLUG}:{eid}" for eid in DROP_IDS] + [derived])
    assert not any(OTHER_SLUG in t for t in deleted)
    patterns = [c.args[0] for c in fake.get_auto_event_ids_like.call_args_list]
    assert sorted(patterns) == sorted(f"ttx:*:{SLUG}:{eid}:*" for eid in DROP_IDS)
    out = capsys.readouterr().out
    assert "共有カレンダーの他グループ行を削除: 7 件" in out
    assert f"{OWNER}: 文翠麗 7" in out


def test_cleanup_agency_rows_only_stored_rows(cleanup_env):
    fake, _ = cleanup_env
    fake.get_future_text_rows.return_value = [
        r for r in _stored_rows() if not r["tweet_id"].endswith("3092306395283615134")]
    assert worker.cleanup_agency_rows() == 6


def test_cleanup_agency_rows_step_summary(cleanup_env, monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    worker.cleanup_agency_rows()
    assert "共有カレンダーの他グループ行を削除: 7 件" in path.read_text(encoding="utf-8")


def test_cleanup_agency_rows_noop_without_names(fake_sb):
    fake_sb.get_future_text_rows.return_value = _stored_rows()
    assert worker.cleanup_agency_rows() == 0
    fake_sb.delete_auto_events.assert_not_called()


def test_cleanup_agency_rows_noop_when_rows_not_list(cleanup_env):
    fake, _ = cleanup_env
    fake.get_future_text_rows.return_value = MagicMock()
    assert worker.cleanup_agency_rows() == 0
    fake.delete_auto_events.assert_not_called()


def test_cleanup_agency_rows_read_failure_not_fatal(cleanup_env, capsys):
    fake, _ = cleanup_env
    fake.get_future_text_rows.side_effect = RuntimeError("読めない")
    assert worker.cleanup_agency_rows() == 0
    assert "読み込みに失敗" in capsys.readouterr().out


def test_cleanup_agency_rows_name_failure_not_fatal(monkeypatch, fake_sb, capsys):
    def boom():
        raise RuntimeError("図鑑を読めない")
    monkeypatch.setattr(worker, "build_idol_name_list", boom)
    assert worker.cleanup_agency_rows() == 0
    assert "登録グループ一覧の取得に失敗" in capsys.readouterr().out


def test_cleanup_agency_rows_delete_failure_not_fatal(cleanup_env, capsys):
    fake, _ = cleanup_env
    fake.delete_auto_events.side_effect = RuntimeError("消せない")
    assert worker.cleanup_agency_rows() == 0
    assert "削除に失敗" in capsys.readouterr().out


def test_cleanup_agency_rows_ttx_lookup_failure_still_deletes(cleanup_env):
    fake, _ = cleanup_env
    fake.get_auto_event_ids_like.side_effect = RuntimeError("like できない")
    assert worker.cleanup_agency_rows() == 7
    deleted = fake.delete_auto_events.call_args.args[0]
    assert sorted(deleted) == sorted(f"tt:{SLUG}:{eid}" for eid in DROP_IDS)


def test_orphan_cleanup_calls_agency_cleanup(monkeypatch, fake_sb):
    called = []
    monkeypatch.setattr(worker, "cleanup_no_event_rows", lambda: called.append("no_event"))
    monkeypatch.setattr(worker, "cleanup_agency_rows", lambda: called.append("agency"))

    def boom():
        raise RuntimeError("図鑑を読めない")
    monkeypatch.setattr(worker, "build_timetree_watch_list", boom)

    assert worker.cleanup_orphan_rows() == 0
    assert called == ["no_event", "agency"]


def test_orphan_cleanup_survives_agency_failure(monkeypatch, fake_sb, capsys):
    monkeypatch.setattr(worker, "cleanup_no_event_rows", lambda: 0)

    def broken():
        raise RuntimeError("判定で落ちた")
    monkeypatch.setattr(worker, "cleanup_agency_rows", broken)

    def boom():
        raise RuntimeError("図鑑を読めない")
    monkeypatch.setattr(worker, "build_timetree_watch_list", boom)

    assert worker.cleanup_orphan_rows() == 0
    assert "共有カレンダーの掃除に失敗" in capsys.readouterr().out
