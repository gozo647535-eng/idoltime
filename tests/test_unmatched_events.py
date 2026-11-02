"""登録グループが出ない公演を番兵行として保存する処理のテスト。

ネットワークにも Supabase にも触れない。sb と取得関数はすべて差し替える。
"""
import os
from unittest.mock import MagicMock

import pytest

# worker / sb は import 時に環境変数を読むので、実環境の設定を持ち込まない
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
             "LIVEPOCKET_FETCH_LIMIT", "LIVEPOCKET_UNMATCHED_FETCH_LIMIT", "YRT_FORCE"):
    os.environ.pop(_key, None)

import worker  # noqa: E402

TODAY = "2026-11-03"
LP_URL = "https://livepocket.jp/e/abc123"
TD_URL = "https://ticketdive.com/event/fes-x"
IDOLS = [{"name": "登録グループ", "idol_slug": "artist:registered"}]


def lp_info(**overrides):
    info = {
        "event_name": "未登録だけの対バン",
        "event_datetime": "2026-11-10 18:00",
        "venue": "会場:渋谷テストホール",
        "sale_datetime": None,
        "is_first_come": True,
        "ticket_types": [{"name": "チケット", "price": "3,000円"}],
        "performers": ["未登録A", "未登録B"],
        "act_text": "未登録A / 未登録B / 他",
        "act_labeled": True,
    }
    info.update(overrides)
    return info


def td_info(day_stages, **overrides):
    info = {
        "event_name": "未登録だけのフェス",
        "event_datetime": f"{day_stages[0]['date']} 12:00",
        "venue": "代表会場",
        "open_time": "11:00",
        "artists": ["初日のグループ"],
        "day_stages": day_stages,
    }
    info.update(overrides)
    return info


@pytest.fixture
def fake_sb(monkeypatch):
    """worker から見える sb を丸ごと差し替え、実際の通信が起きないようにする。"""
    fake = MagicMock()
    fake.get_last_seen.return_value = None
    fake.get_auto_event_ids.return_value = []
    monkeypatch.setattr(worker, "sb", fake)
    return fake


def ingest(monkeypatch, fake_sb, info, source="livepocket", url=LP_URL, idols=IDOLS):
    """_ingest_sale_url を1回動かし、(返り値, 書いた行, touched) を返す。"""
    monkeypatch.setattr(worker.ticketsite, "fetch_event", lambda _url: info)
    monkeypatch.setattr(worker, "_td_info", lambda _url: info)
    touched = set()
    result = worker._ingest_sale_url(source, url, idols, TODAY, touched)
    written = [r for call in fake_sb.upsert_auto_events.call_args_list for r in call.args[0]]
    return result, written, touched


# --- (a) 一致ゼロ・条件充足 → 番兵行1行 ---

def test_no_match_saves_one_sentinel_row(monkeypatch, fake_sb, capsys):
    result, written, touched = ingest(monkeypatch, fake_sb, lp_info())

    assert result == (1, False)
    assert len(written) == 1
    row = written[0]
    assert row["tweet_id"] == "yrt:unmatched:lp-abc123:20261110"
    assert row["idol_slug"] == worker.UNMATCHED_SLUG == "unmatched"
    assert row["event_name"] == "未登録だけの対バン"
    assert row["event_datetime"] == "2026-11-10 18:00"
    assert row["other_details"] == "渋谷テストホール"
    assert row["raw_text"] == "未登録A / 未登録B"
    assert row["ticket_url"] == LP_URL
    assert row["is_first_come"] is True
    assert touched == {"unmatched"}
    assert "未登録公演として保存: 未登録だけの対バン (2026-11-10)" in capsys.readouterr().out


def test_raw_text_falls_back_to_performers(monkeypatch, fake_sb):
    _, written, _ = ingest(monkeypatch, fake_sb, lp_info(act_text=""))
    assert written[0]["raw_text"] == "未登録A / 未登録B"


def test_raw_text_is_none_without_any_lineup(monkeypatch, fake_sb):
    _, written, _ = ingest(monkeypatch, fake_sb, lp_info(act_text="", performers=[]))
    assert len(written) == 1
    assert written[0]["raw_text"] is None


def test_today_is_not_past(monkeypatch, fake_sb):
    _, written, _ = ingest(monkeypatch, fake_sb, lp_info(event_datetime=f"{TODAY} 18:00"))
    assert [r["tweet_id"] for r in written] == ["yrt:unmatched:lp-abc123:20261103"]


def test_single_day_ticketdive_uses_artists(monkeypatch, fake_sb):
    info = td_info([{"date": "2026-11-10", "venue": "会場A", "open_time": None,
                     "artists": ["未登録A", "未登録B"]}],
                   artists=["未登録A", "未登録B"])
    _, written, _ = ingest(monkeypatch, fake_sb, info, source="ticketdive", url=TD_URL)
    assert len(written) == 1
    assert written[0]["tweet_id"] == "yrt:unmatched:fes-x:20261110"
    assert written[0]["raw_text"] == "未登録A / 未登録B"


# --- raw_text の優先順位 ---

DT = "2026-11-10"
LONG_TEXT = "公演の説明" * 100


def test_lineup_text_prefers_performers_over_long_act_text():
    info = {"act_text": LONG_TEXT, "performers": ["未登録A", "未登録B"]}
    assert worker._unmatched_lineup_text(info, DT) == "未登録A / 未登録B"


def test_lineup_text_prefers_performers_over_short_act_text():
    info = {"act_text": "短い自由文", "performers": ["未登録A"]}
    assert worker._unmatched_lineup_text(info, DT) == "未登録A"


def test_lineup_text_is_none_with_only_long_act_text():
    info = {"act_text": LONG_TEXT, "performers": []}
    assert worker._unmatched_lineup_text(info, DT) is None


def test_lineup_text_uses_short_act_text_when_no_performers():
    info = {"act_text": "未登録A ほか", "performers": []}
    assert worker._unmatched_lineup_text(info, DT) == "未登録A ほか"


@pytest.mark.parametrize("length, expected_used", [
    (worker.LINEUP_TEXT_MAX, True),
    (worker.LINEUP_TEXT_MAX + 1, False),
])
def test_lineup_text_max_is_inclusive(length, expected_used):
    text = "あ" * length
    expected = text if expected_used else None
    assert worker._unmatched_lineup_text({"act_text": text}, DT) == expected


def test_lineup_text_day_stages_win_over_everything():
    info = {
        "act_text": "短い自由文",
        "performers": ["未登録A"],
        "artists": ["初日のグループ"],
        "day_stages": [
            {"date": DT, "artists": ["その日のグループ", "その日のグループ", "別のグループ"]},
            {"date": "2026-11-11", "artists": ["翌日のグループ"]},
        ],
    }
    assert worker._unmatched_lineup_text(info, DT) == "その日のグループ / 別のグループ"


def test_lineup_text_day_without_artists_does_not_borrow_other_fields():
    info = {
        "act_text": "短い自由文",
        "performers": ["未登録A"],
        "artists": ["初日のグループ"],
        "day_stages": [{"date": "2026-11-11", "artists": ["翌日のグループ"]}],
    }
    assert worker._unmatched_lineup_text(info, DT) is None


def test_lineup_text_uses_artists_without_day_stages():
    info = {"artists": ["未登録A", "未登録B"], "day_stages": []}
    assert worker._unmatched_lineup_text(info, DT) == "未登録A / 未登録B"


# --- (b) 一致あり → 番兵行なし ---

def test_match_writes_no_sentinel_row(monkeypatch, fake_sb):
    info = lp_info(performers=["登録グループ", "未登録A"], act_text="登録グループ / 未登録A")
    result, written, touched = ingest(monkeypatch, fake_sb, info)

    assert result == (1, False)
    assert [r["idol_slug"] for r in written] == ["artist:registered"]
    assert written[0]["raw_text"] is None
    assert touched == {"artist:registered"}


def test_match_removes_sentinel_left_by_earlier_read(monkeypatch, fake_sb):
    old_id = "yrt:unmatched:lp-abc123:20261110"
    fake_sb.get_auto_event_ids.return_value = [old_id]
    info = lp_info(performers=["登録グループ"], act_text="登録グループ")

    ingest(monkeypatch, fake_sb, info)

    fake_sb.get_auto_event_ids.assert_called_once_with(
        "unmatched", prefix="yrt:unmatched:lp-abc123:")
    fake_sb.delete_auto_events.assert_called_once_with([old_id])


def test_sentinel_is_kept_when_writing_real_rows_fails(monkeypatch, fake_sb):
    fake_sb.upsert_auto_events.side_effect = RuntimeError("down")
    info = lp_info(performers=["登録グループ"], act_text="登録グループ")

    result, _, _ = ingest(monkeypatch, fake_sb, info)

    assert result == (0, False)
    fake_sb.delete_auto_events.assert_not_called()


def test_failure_removing_sentinel_does_not_change_result(monkeypatch, fake_sb):
    fake_sb.get_auto_event_ids.side_effect = RuntimeError("down")
    info = lp_info(performers=["登録グループ"], act_text="登録グループ")
    result, _, _ = ingest(monkeypatch, fake_sb, info)
    assert result == (1, False)


def test_no_match_does_not_look_for_stale_sentinels(monkeypatch, fake_sb):
    ingest(monkeypatch, fake_sb, lp_info())
    fake_sb.delete_auto_events.assert_not_called()


# --- (c)(d) 条件を満たさない → 保存しない(理由をログに1行) ---

@pytest.mark.parametrize("overrides, reason", [
    ({"venue": None}, "会場なし"),
    ({"venue": ""}, "会場なし"),
    ({"venue": "  \n "}, "会場なし"),
    ({"venue": "会場:"}, "会場なし"),
    ({"event_datetime": "2026-11-02 18:00"}, "公演日が過去"),
    ({"event_name": None}, "公演名なし"),
    ({"event_name": "  "}, "公演名なし"),
    ({"event_datetime": None}, "公演日なし"),
    ({"event_datetime": "調整中"}, "公演日なし"),
])
def test_incomplete_event_is_not_saved(monkeypatch, fake_sb, capsys, overrides, reason):
    result, written, touched = ingest(monkeypatch, fake_sb, lp_info(**overrides))

    assert result == (0, False)
    assert written == []
    assert touched == set()
    logged = [line for line in capsys.readouterr().out.splitlines() if "未登録公演" in line]
    assert len(logged) == 1
    assert reason in logged[0]


def test_cached_page_is_not_read_again(monkeypatch, fake_sb):
    fake_sb.get_last_seen.return_value = TODAY
    fetch = MagicMock()
    monkeypatch.setattr(worker.ticketsite, "fetch_event", fetch)

    assert worker._ingest_sale_url("livepocket", LP_URL, IDOLS, TODAY, set()) == (0, True)
    fetch.assert_not_called()
    fake_sb.upsert_auto_events.assert_not_called()


# --- 複数日開催 ---

MULTI_DAY = [
    {"date": "2026-11-01", "venue": "過去の会場", "open_time": "10:00", "artists": ["過去の出演者"]},
    {"date": "2026-11-10", "venue": "仙台の会場", "open_time": "11:00", "artists": ["未登録A"]},
    {"date": "2026-11-11", "venue": "大阪の会場", "open_time": "12:00", "artists": ["未登録B"]},
]


def test_multi_day_saves_one_row_per_upcoming_day(monkeypatch, fake_sb):
    info = td_info(MULTI_DAY)
    result, written, touched = ingest(monkeypatch, fake_sb, info, source="ticketdive", url=TD_URL)

    assert result == (2, False)
    assert [r["tweet_id"] for r in written] == [
        "yrt:unmatched:fes-x:20261110", "yrt:unmatched:fes-x:20261111"]
    assert [r["event_datetime"] for r in written] == ["2026-11-10", "2026-11-11"]
    assert [r["other_details"] for r in written] == ["仙台の会場", "大阪の会場"]
    assert [r["open_time"] for r in written] == ["11:00", "12:00"]
    assert [r["raw_text"] for r in written] == ["未登録A", "未登録B"]
    assert touched == {"unmatched"}


def test_multi_day_all_past_is_not_saved(monkeypatch, fake_sb):
    info = td_info(MULTI_DAY[:1] + [dict(MULTI_DAY[1], date="2026-11-02")])
    result, written, _ = ingest(monkeypatch, fake_sb, info, source="ticketdive", url=TD_URL)
    assert result == (0, False)
    assert written == []


def test_multi_day_uses_main_venue_when_day_has_none(monkeypatch, fake_sb):
    info = td_info([dict(MULTI_DAY[1], venue=None), MULTI_DAY[2]])
    _, written, _ = ingest(monkeypatch, fake_sb, info, source="ticketdive", url=TD_URL)
    assert [r["other_details"] for r in written] == ["代表会場", "大阪の会場"]


def test_multi_day_skips_only_days_without_any_venue(monkeypatch, fake_sb):
    info = td_info([dict(MULTI_DAY[1], venue=None), MULTI_DAY[2]], venue=None)
    _, written, _ = ingest(monkeypatch, fake_sb, info, source="ticketdive", url=TD_URL)
    assert [r["tweet_id"] for r in written] == ["yrt:unmatched:fes-x:20261111"]


def test_multi_day_with_registered_group_writes_no_sentinel(monkeypatch, fake_sb):
    stages = [MULTI_DAY[1], dict(MULTI_DAY[2], artists=["登録グループ"])]
    _, written, _ = ingest(monkeypatch, fake_sb, td_info(stages), source="ticketdive", url=TD_URL)
    assert [r["idol_slug"] for r in written] == ["artist:registered"]


# --- 過去分の掃除 ---

def test_past_sentinel_rows_are_cleaned_up(fake_sb):
    fake_sb.get_auto_event_ids.return_value = [
        "yrt:unmatched:lp-old:20261102", "yrt:unmatched:lp-new:20261103"]

    assert worker._cleanup_past_yrt(worker.UNMATCHED_SLUG, TODAY) == 1

    fake_sb.get_auto_event_ids.assert_called_once_with("unmatched", prefix="yrt:unmatched:")
    fake_sb.delete_auto_event.assert_called_once_with("yrt:unmatched:lp-old:20261102")


# --- (e) run_livepocket の未一致枠 ---

def card(name, status="販売中"):
    return {"name": name, "url": f"https://livepocket.jp/e/{name}", "status": status,
            "performers": []}


@pytest.fixture
def run_lp(monkeypatch, fake_sb):
    """一覧を差し替えて run_livepocket を動かし、取りに行った URL の順を返す。"""
    def run(cards, skipped=()):
        monkeypatch.delenv("LIVEPOCKET_LISTING", raising=False)
        monkeypatch.setattr(worker.ticketsite, "livepocket_listing", lambda pages: cards)
        monkeypatch.setattr(worker, "build_idol_name_list", lambda: IDOLS)
        monkeypatch.setattr(worker.lineup, "build_index", lambda idols: None)
        monkeypatch.setattr(
            worker.lineup, "names_in_text",
            lambda text, idx: [("登録", "artist:registered")] if text.startswith("hit") else [])
        calls = []

        def fake_ingest(source, url, idols, today, touched):
            assert source == "livepocket"
            calls.append(url.rsplit("/", 1)[-1])
            if url.rsplit("/", 1)[-1] in skipped:
                return 0, True
            return 1, False

        monkeypatch.setattr(worker, "_ingest_sale_url", fake_ingest)
        return calls, worker.run_livepocket()
    return run


def test_default_unmatched_limit_is_20():
    assert worker.LIVEPOCKET_UNMATCHED_FETCH_LIMIT == 20


def test_unmatched_cards_stop_at_limit(monkeypatch, run_lp, capsys):
    monkeypatch.setattr(worker, "LIVEPOCKET_UNMATCHED_FETCH_LIMIT", 2)
    cards = [card(f"miss{i}") for i in range(5)] + [card("hit0")]

    calls, total = run_lp(cards)

    assert calls == ["hit0", "miss0", "miss1"]
    assert total == 3
    assert "未一致分の取得 2 件" in capsys.readouterr().out


def test_unmatched_limit_is_separate_from_matched_limit(monkeypatch, run_lp):
    monkeypatch.setattr(worker, "LIVEPOCKET_FETCH_LIMIT", 2)
    monkeypatch.setattr(worker, "LIVEPOCKET_UNMATCHED_FETCH_LIMIT", 1)
    cards = [card(f"hit{i}") for i in range(4)] + [card(f"miss{i}") for i in range(3)]

    calls, _ = run_lp(cards)

    # 一致分の上限からあふれた hit2, hit3 は未一致枠に回らない
    assert calls == ["hit0", "hit1", "miss0"]


def test_matched_order_keeps_pre_sale_first_and_precedes_unmatched(monkeypatch, run_lp):
    cards = [card("miss_on"), card("hit_on"), card("miss_pre", "販売前"), card("hit_pre", "販売前")]
    calls, _ = run_lp(cards)
    assert calls == ["hit_pre", "hit_on", "miss_pre", "miss_on"]


def test_zero_limit_disables_unmatched_cards(monkeypatch, run_lp):
    monkeypatch.setattr(worker, "LIVEPOCKET_UNMATCHED_FETCH_LIMIT", 0)
    calls, _ = run_lp([card("miss0"), card("hit0")])
    assert calls == ["hit0"]


def test_summary_excludes_cards_already_read_today(run_lp, capsys):
    run_lp([card("miss0"), card("miss1")], skipped={"miss0"})
    assert "未一致分の取得 1 件" in capsys.readouterr().out
