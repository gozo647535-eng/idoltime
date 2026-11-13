"""LivePocket の発売取りこぼし対策のテスト(LP-SALE-DISCOVERY-DESIGN.md §5)。

一覧とイベントページの解析、run_livepocket の上限の切り方(案B)、開催日順巡回の
試運転(案F)を確かめる。ネットワークにも Supabase にも触れない。
"""
import datetime
import os
from unittest.mock import MagicMock

import pytest

# worker / sb は import 時に環境変数を読むので、実環境の設定を持ち込まない
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
             "LIVEPOCKET_PAGES", "LIVEPOCKET_FETCH_LIMIT", "LIVEPOCKET_UNMATCHED_FETCH_LIMIT",
             "LIVEPOCKET_UNMATCHED_RECHECK_DAYS", "LP_SWEEP_DAYS", "LP_SWEEP_MAX_PAGES",
             "LP_SWEEP_RETRY_DAYS", "LP_SWEEP_MAX_SPAN_DAYS", "LP_SWEEP_PROBE_PAGES", "YRT_FORCE"):
    os.environ.pop(_key, None)

import ticketsite  # noqa: E402
import worker  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "livepocket")
IDOLS = [{"name": "登録グループ", "idol_slug": "artist:registered"}]


def fixture_html(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def day(offset=0):
    """今日(JST)から offset 日後の YYYY-MM-DD。"""
    now = datetime.datetime.now(worker.JST)
    return (now + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")


def lp_url(name):
    return f"https://livepocket.jp/e/{name}"


def cache_key(name):
    return worker._yrt_cache_key("livepocket", lp_url(name))


@pytest.fixture
def fake_sb(monkeypatch):
    fake = MagicMock()
    fake.get_last_seen.return_value = None
    fake.get_auto_event_ids.return_value = []
    fake.get_last_seen_by_prefix.return_value = {}
    fake.get_ticket_page_rows.return_value = []
    monkeypatch.setattr(worker, "sb", fake)
    return fake


# --- パーサ ---

def test_listing_fixture_has_only_the_20_main_cards(monkeypatch):
    html = fixture_html("livepocket-listing-p1.html")
    monkeypatch.setattr(ticketsite, "_get_listing", lambda url: html)

    cards, problem = ticketsite.livepocket_listing_page(1)

    assert problem is None
    # 下部の「ピックアップ」「新着イベント」のカードは数えない
    assert len(cards) == 20
    assert len({c["url"] for c in cards}) == 20
    assert cards[0]["url"] == "https://livepocket.jp/e/otonarivol16"
    assert cards[0]["status"] == "販売中"
    assert "ヒカリトナリ" in cards[0]["performers"]
    assert cards[2]["url"] == "https://livepocket.jp/e/lo1ge"
    assert cards[2]["status"] == "販売前"


def test_listing_fixture_reads_start_dates(monkeypatch):
    html = fixture_html("livepocket-listing-p1.html")
    monkeypatch.setattr(ticketsite, "_get_listing", lambda url: html)

    cards, _ = ticketsite.livepocket_listing_page(1)

    assert cards[0]["start_date"] == "2026-11-16"
    # 「2026年12月27日(日)<br>〜2026年12月27日(日)」の範囲表記
    assert "〜" in cards[1]["date"]
    assert cards[1]["start_date"] == "2026-12-27"
    assert all(c["start_date"] for c in cards)


@pytest.mark.parametrize("text, expected", [
    ("2026年11月16日(月)", "2026-11-16"),
    ("2026年11月21日(土) 〜2026年11月22日(日)", "2026-11-21"),
    ("2027年1月7日(木)", "2027-01-07"),
    ("日程未定", None),
    ("2026年13月40日", None),
    ("", None),
    (None, None),
])
def test_listing_start_date(text, expected):
    assert ticketsite.listing_start_date(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("2026年11月16日(月)", None),
    ("2026年11月21日(土) 〜2026年11月22日(日)", "2026-11-22"),
    ("2026年8月15日(土)<br>〜2026年12月31日(木)", "2026-12-31"),
    ("2026年12月30日(水) 〜2027年1月3日(日)", "2027-01-03"),
    ("2026年11月21日(土) 〜2026年13月40日", None),
    ("日程未定", None),
    ("", None),
    (None, None),
])
def test_listing_end_date(text, expected):
    assert ticketsite.listing_end_date(text) == expected


def test_listing_fixture_reads_end_date_of_range(monkeypatch):
    html = fixture_html("livepocket-listing-p1.html")
    monkeypatch.setattr(ticketsite, "_get_listing", lambda url: html)

    cards, _ = ticketsite.livepocket_listing_page(1)

    # 「2026年12月27日(日)<br>〜2026年12月27日(日)」の範囲表記
    assert cards[1]["end_date"] == "2026-12-27"
    assert all("end_date" in c for c in cards)


def test_card_with_unreadable_date_does_not_raise(monkeypatch):
    html = ('<li class="event-card-list__item"><a href="/e/nodate">'
            '<span class="event-card__tag">販売前</span>'
            '<p><span class="event-card__date">日程</span> 調整中</p></a>')
    monkeypatch.setattr(ticketsite, "_get_listing", lambda url: html)

    cards, problem = ticketsite.livepocket_listing_page(1)

    assert problem is None
    assert cards[0]["url"] == "https://livepocket.jp/e/nodate"
    assert cards[0]["start_date"] is None


def test_listing_page_url_uses_sort_and_page(monkeypatch):
    urls = []
    monkeypatch.setattr(ticketsite, "_get_listing", lambda url: urls.append(url))

    ticketsite.livepocket_listing_page(1, sort=2)
    ticketsite.livepocket_listing_page(3, sort=2)

    assert urls[0].endswith("&sort=2")
    assert urls[1].endswith("&sort=2&page=3")


@pytest.mark.parametrize("html, expected", [
    (None, "no_response"),
    ("", "no_response"),
    ("<html><title>x</title><script>gokuProps</script></html>", "waf"),
    ("<html><title>同意</title></html>", "no_cards"),
])
def test_listing_page_reports_problem(monkeypatch, html, expected):
    monkeypatch.setattr(ticketsite, "_get_listing", lambda url: html)
    assert ticketsite.livepocket_listing_page(1) == ([], expected)


def test_listing_keeps_pages_call_and_stops_at_first_problem(monkeypatch):
    html = fixture_html("livepocket-listing-p1.html")
    urls = []

    def fake_get(url):
        urls.append(url)
        return html if len(urls) == 1 else None

    monkeypatch.setattr(ticketsite, "_get_listing", fake_get)

    cards = ticketsite.livepocket_listing(pages=3)

    assert len(cards) == 20
    assert urls == [ticketsite.LIVEPOCKET_IDOL_LIST, ticketsite.LIVEPOCKET_IDOL_LIST + "&page=2"]
    assert ticketsite.LIVEPOCKET_IDOL_LIST.endswith("&sort=3")


def test_event_page_8iavg(monkeypatch):
    html = fixture_html("livepocket-8iavg.html")
    monkeypatch.setattr(ticketsite, "_get", lambda url: html)

    info = ticketsite.fetch_event("https://livepocket.jp/e/8iavg")

    assert info["sale_datetime"] == "2026-11-09 20:00:00"
    assert info["is_first_come"] is True
    assert info["performers"] == ["透色ドロップ"]


def _sale_lines(*blocks):
    """LivePocket の受付ブロックの並び(実ページと同じ行構成)を組み立てる。"""
    lines = []
    for kind, start, end in blocks:
        if kind:
            lines += ["販売中", kind, f"{kind}販売受付"]
        lines += ["販売受付期間", start, f"〜{end}"]
    return lines


def test_livepocket_sale_prefers_first_come_even_if_lottery_comes_first():
    # 抽選ブロックが先に並ぶページでも、先着の開始日時を発売日時にする
    lines = _sale_lines(
        ("抽選", "2026年10月30日(金) 20:00", "2026年11月7日(土) 23:59"),
        ("先着", "2026年11月9日(月) 20:00", "2026年11月14日(土) 12:45"),
    )
    assert ticketsite._livepocket_sale(lines) == "2026-11-09 20:00:00"


def test_livepocket_sale_lottery_only_page_keeps_lottery_start():
    lines = _sale_lines(
        ("抽選", "2026年10月30日(金) 20:00", "2026年11月7日(土) 23:59"),
    )
    assert ticketsite._livepocket_sale(lines) == "2026-10-30 20:00:00"


def test_livepocket_sale_without_labels_falls_back_to_first_block():
    lines = _sale_lines(
        (None, "2026年10月30日(金) 20:00", "2026年11月7日(土) 23:59"),
        (None, "2026年11月9日(月) 20:00", "2026年11月14日(土) 12:45"),
    )
    assert ticketsite._livepocket_sale(lines) == "2026-10-30 20:00:00"


# --- 案B: run_livepocket の上限の切り方 ---

def lp_card(name, status="販売中"):
    return {"name": name, "url": lp_url(name), "status": status, "performers": []}


@pytest.fixture
def run_lp(monkeypatch, fake_sb):
    """一覧と保存済みの読み取り日を差し替えて run_livepocket を動かし、読んだ順を返す。"""
    def run(cards, last=None):
        monkeypatch.delenv("LIVEPOCKET_LISTING", raising=False)
        fake_sb.get_last_seen_by_prefix.return_value = last if last is not None else {}
        monkeypatch.setattr(worker.ticketsite, "livepocket_listing", lambda pages: cards)
        monkeypatch.setattr(worker, "build_idol_name_list", lambda: IDOLS)
        monkeypatch.setattr(worker.lineup, "build_index", lambda idols: None)
        monkeypatch.setattr(
            worker.lineup, "names_in_text",
            lambda text, idx: [("登録", "artist:registered")] if text.startswith("hit") else [])
        calls = []

        def fake_ingest(source, url, idols, today, touched):
            calls.append(url.rsplit("/", 1)[-1])
            return 1, False

        monkeypatch.setattr(worker, "_ingest_sale_url", fake_ingest)
        return calls, worker.run_livepocket()
    return run


def test_default_unmatched_recheck_days_is_3():
    assert worker.LIVEPOCKET_UNMATCHED_RECHECK_DAYS == 3


def test_cards_read_today_do_not_use_up_the_limit(monkeypatch, run_lp, capsys):
    monkeypatch.setattr(worker, "LIVEPOCKET_FETCH_LIMIT", 25)
    cards = [lp_card(f"hit{i}") for i in range(27)]
    last = {cache_key(f"hit{i}"): day() for i in range(25)}

    calls, total = run_lp(cards, last)

    assert calls == ["hit25", "hit26"]
    assert total == 2
    out = capsys.readouterr().out
    assert "登録グループが出る 対象 2 件 / 読んだ 2 件 / 上限で読まなかった 0 件" in out
    assert "当日取得済みで対象外 25 件" in out


def test_target_read_on_earlier_day_is_read_again(run_lp):
    calls, _ = run_lp([lp_card("hit0")], {cache_key("hit0"): day(-1)})
    assert calls == ["hit0"]


def test_unmatched_reads_unread_cards_first(monkeypatch, run_lp):
    monkeypatch.setattr(worker, "LIVEPOCKET_UNMATCHED_FETCH_LIMIT", 2)
    cards = [lp_card("miss_old"), lp_card("miss_older"), lp_card("miss_new")]
    last = {cache_key("miss_old"): day(-5), cache_key("miss_older"): day(-9)}

    calls, _ = run_lp(cards, last)

    assert calls == ["miss_new", "miss_older"]


def test_unmatched_recheck_interval(monkeypatch, run_lp, capsys):
    monkeypatch.setattr(worker, "LIVEPOCKET_UNMATCHED_RECHECK_DAYS", 3)
    cards = [lp_card("miss_today"), lp_card("miss_3d"), lp_card("miss_4d")]
    last = {cache_key("miss_today"): day(), cache_key("miss_3d"): day(-3),
            cache_key("miss_4d"): day(-4)}

    calls, _ = run_lp(cards, last)

    assert calls == ["miss_4d"]
    assert "再確認の間隔内で対象外 2 件" in capsys.readouterr().out


def test_limits_are_not_exceeded(monkeypatch, run_lp, capsys):
    monkeypatch.setattr(worker, "LIVEPOCKET_FETCH_LIMIT", 2)
    monkeypatch.setattr(worker, "LIVEPOCKET_UNMATCHED_FETCH_LIMIT", 1)
    cards = [lp_card(f"hit{i}") for i in range(5)] + [lp_card(f"miss{i}") for i in range(3)]

    calls, _ = run_lp(cards)

    assert calls == ["hit0", "hit1", "miss0"]
    out = capsys.readouterr().out
    assert "登録グループが出る 対象 5 件 / 読んだ 2 件 / 上限で読まなかった 3 件" in out
    assert "登録グループが出ない 対象 3 件 / 読んだ 1 件 / 上限で読まなかった 2 件" in out
    # 既存の完了ログの意味は変えない
    assert "未一致分の取得 1 件" in out


def test_last_seen_failure_keeps_previous_behaviour(monkeypatch, run_lp, fake_sb):
    monkeypatch.setattr(worker, "LIVEPOCKET_FETCH_LIMIT", 1)
    monkeypatch.setattr(worker, "LIVEPOCKET_UNMATCHED_FETCH_LIMIT", 1)
    fake_sb.get_last_seen_by_prefix.side_effect = RuntimeError("接続できない")

    calls, _ = run_lp([lp_card("hit0"), lp_card("hit1"), lp_card("miss0"), lp_card("miss1")])

    assert calls == ["hit0", "miss0"]


def test_last_seen_not_a_dict_is_treated_as_empty(run_lp):
    calls, _ = run_lp([lp_card("hit0"), lp_card("miss0")], last=MagicMock())
    assert calls == ["hit0", "miss0"]


def test_yrt_force_does_not_filter(monkeypatch, run_lp, fake_sb):
    monkeypatch.setenv("YRT_FORCE", "1")
    cards = [lp_card("hit0"), lp_card("miss0")]
    last = {cache_key("hit0"): day(), cache_key("miss0"): day()}

    calls, _ = run_lp(cards, last)

    assert calls == ["hit0", "miss0"]
    fake_sb.get_last_seen_by_prefix.assert_not_called()


# --- 案F: 開催日順巡回の試運転 ---

def sweep_card(name, date, status="販売前", performers=(), end=None):
    """name に hit を含むか performers に hit があると、登録グループに当たる扱いになる。"""
    return {"name": name, "url": lp_url(name), "status": status, "date": date or "調整中",
            "start_date": date, "end_date": end, "venue": "", "performers": list(performers)}


@pytest.fixture
def sweep(monkeypatch, fake_sb):
    """pages の各要素はカードのリスト、問題の文字列、例外のいずれか。

    (結果の dict, 読みに行ったページ番号, _ingest_sale_url の差し替え) を返す。
    探り読み(LP_SWEEP_PROBE_PAGES)は既定で切っておき、確かめるテストだけが設定する。
    """
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", ())

    def run(pages, rows=(), last=None):
        monkeypatch.delenv("LIVEPOCKET_LISTING", raising=False)
        fake_sb.get_ticket_page_rows.return_value = list(rows)
        fake_sb.get_last_seen_by_prefix.return_value = last if last is not None else {}
        requested = []

        def fake_page(page, sort):
            assert sort == 2
            requested.append(page)
            entry = pages[page - 1] if page <= len(pages) else "no_cards"
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, str):
                return [], entry
            return entry, None

        ingest = MagicMock()
        monkeypatch.setattr(worker.ticketsite, "livepocket_listing_page", fake_page)
        monkeypatch.setattr(worker.ticketsite, "fetch_event", MagicMock())
        monkeypatch.setattr(worker, "_ingest_sale_url", ingest)
        monkeypatch.setattr(worker, "build_idol_name_list", lambda: IDOLS)
        monkeypatch.setattr(worker.lineup, "build_index", lambda idols: {"x": 1})
        monkeypatch.setattr(worker.lineup, "names_in_text",
                            lambda text, idx: [("登録", "artist:registered")] if "hit" in text else [])
        return worker.run_livepocket_sweep(), requested, ingest
    return run


def target_names(report):
    return [c["name"] for c in report["targets"]]


def test_sweep_defaults():
    assert worker.LP_SWEEP_DAYS == 14
    assert worker.LP_SWEEP_MAX_PAGES == 20
    assert worker.LP_SWEEP_RETRY_DAYS == 3


def test_sweep_stops_when_date_exceeds_range(sweep):
    pages = [
        [sweep_card("hit_today", day()), sweep_card("hit_edge", day(14))],
        [sweep_card("hit_edge2", day(14)), sweep_card("hit_over", day(15))],
        [sweep_card("hit_never", day(15))],
    ]

    report, requested, _ = sweep(pages)

    assert requested == [1, 2]
    assert report["stop"] == "date_limit"
    assert report["interrupted"] is False
    assert target_names(report) == ["hit_today", "hit_edge", "hit_edge2"]
    assert report["excluded"]["beyond_range"] == 1


def test_sweep_stops_at_max_pages(monkeypatch, sweep):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 3)
    pages = [[sweep_card(f"hit{i}", day(1))] for i in range(5)]

    report, requested, _ = sweep(pages)

    assert requested == [1, 2, 3]
    assert report["stop"] == "max_pages"
    assert report["interrupted"] is False
    assert target_names(report) == ["hit0", "hit1", "hit2"]


@pytest.mark.parametrize("failure, stop", [
    ("no_cards", "no_cards"),
    ("waf", "waf"),
    ("no_response", "no_response"),
    (RuntimeError("壊れた"), "exception"),
])
def test_sweep_continues_with_pages_read_before_a_failure(sweep, failure, stop):
    pages = [[sweep_card("hit0", day(1))], failure, [sweep_card("hit_never", day(2))]]

    report, requested, _ = sweep(pages)

    assert requested == [1, 2]
    assert report["stop"] == stop
    assert report["interrupted"] is True
    assert target_names(report) == ["hit0"]


def test_sweep_failure_on_first_page_reports_nothing_to_do(sweep):
    report, requested, _ = sweep(["waf"])
    assert requested == [1]
    assert report["targets"] == []
    assert report["pages"] == []
    assert report["interrupted"] is True


def test_sweep_unreadable_date_is_excluded_and_does_not_stop(sweep, capsys):
    pages = [
        [sweep_card("hit_nodate", None), sweep_card("hit0", day(1))],
        [sweep_card("hit1", day(2)), sweep_card("hit_over", day(15))],
    ]

    report, requested, _ = sweep(pages)

    assert requested == [1, 2]
    assert target_names(report) == ["hit0", "hit1"]
    assert report["excluded"]["date_unreadable"] == 1
    assert "開催日が読めない 1 件" in capsys.readouterr().out


def test_sweep_records_first_and_last_date_of_each_page(sweep):
    pages = [[sweep_card("hit_nodate", None), sweep_card("hit0", day(1)), sweep_card("hit1", day(3))]]

    report, _, _ = sweep(pages)

    assert report["pages"][0] == {"page": 1, "cards": 3, "first": day(1), "last": day(3)}


def test_sweep_counts_a_repeated_card_once(sweep):
    pages = [[sweep_card("hit0", day(1))], [sweep_card("hit0", day(1)), sweep_card("hit1", day(2))]]

    report, _, _ = sweep(pages)

    assert target_names(report) == ["hit0", "hit1"]
    assert report["excluded"]["duplicate"] == 1


@pytest.mark.parametrize("status, kept", [
    ("販売前", True), ("販売中", True), ("売切", False), ("販売終了", False),
])
def test_sweep_keeps_only_cards_on_or_before_sale(sweep, status, kept):
    report, _, _ = sweep([[sweep_card("hit0", day(1), status=status)]])
    assert target_names(report) == (["hit0"] if kept else [])
    assert report["excluded"]["status"] == (0 if kept else 1)


def test_sweep_matches_groups_in_name_or_performers(sweep):
    pages = [[sweep_card("miss0", day(1)),
              sweep_card("hit_in_name", day(1)),
              sweep_card("in_cast", day(1), performers=["hit"])]]

    report, _, _ = sweep(pages)

    assert target_names(report) == ["hit_in_name", "in_cast"]
    assert report["excluded"]["no_group"] == 1


def test_sweep_excludes_only_cards_with_a_real_row(sweep, fake_sb):
    pages = [[sweep_card("hit_real", day(1)), sweep_card("hit_sentinel", day(1)),
              sweep_card("hit_short", day(1)), sweep_card("hit_none", day(1))]]
    rows = [
        {"tweet_id": "yrt:artist:x:lp-hit_real:20261120", "ticket_url": lp_url("hit_real")},
        {"tweet_id": "yrt:unmatched:lp-hit_sentinel:20261120", "ticket_url": lp_url("hit_sentinel")},
        # 同じページでも URL の形が違うことがある
        {"tweet_id": "yrt:artist:x:lp-hit_short:20261120",
         "ticket_url": "https://t.livepocket.jp/e/hit_short"},
    ]

    report, _, _ = sweep(pages, rows=rows)

    assert target_names(report) == ["hit_sentinel", "hit_none"]
    assert report["excluded"]["has_row"] == 2
    fake_sb.get_ticket_page_rows.assert_called_once_with(day(), prefix="yrt:")


def test_sweep_excludes_cards_read_within_retry_days(sweep):
    pages = [[sweep_card("hit_today", day(1)), sweep_card("hit_3d", day(1)),
              sweep_card("hit_4d", day(1)), sweep_card("hit_unread", day(1))]]
    last = {cache_key("hit_today"): day(), cache_key("hit_3d"): day(-3),
            cache_key("hit_4d"): day(-4)}

    report, _, _ = sweep(pages, last=last)

    assert target_names(report) == ["hit_4d", "hit_unread"]
    assert report["excluded"]["recent"] == 2


def test_sweep_trial_does_not_write_or_fetch_event_pages(sweep, fake_sb):
    report, _, ingest = sweep([[sweep_card("hit0", day(1))]])

    assert target_names(report) == ["hit0"]
    ingest.assert_not_called()
    worker.ticketsite.fetch_event.assert_not_called()
    fake_sb.upsert_auto_events.assert_not_called()
    assert {c[0] for c in fake_sb.method_calls} == {"get_ticket_page_rows", "get_last_seen_by_prefix"}


def test_sweep_writes_step_summary(sweep, monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    pages = [[sweep_card("hit|0", day(1)), sweep_card("hit1", day(2))], "waf"]

    sweep(pages)

    text = path.read_text(encoding="utf-8")
    assert "試運転" in text
    assert "途中停止: WAF の認証ページ" in text
    assert "途中で止まったか: はい" in text
    assert f"| 1 | 2 | {day(1)} | {day(2)} |" in text
    assert f"| {day(2)} | hit1 | {lp_url('hit1')} |" in text
    assert "hit\\|0" in text


def test_sweep_logs_targets(sweep, capsys):
    sweep([[sweep_card("hit0", day(1))]])
    out = capsys.readouterr().out
    assert "試運転" in out
    assert f"{day(1)} hit0 {lp_url('hit0')}" in out


def test_sweep_counts_past_dated_cards(sweep):
    report, _, _ = sweep([[sweep_card("hit_past", day(-2)), sweep_card("hit0", day(1))]])
    assert report["past"] == 1


def test_sweep_disabled_by_listing_switch(fake_sb, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("読みに行かないはず")

    monkeypatch.setenv("LIVEPOCKET_LISTING", "0")
    monkeypatch.setattr(worker.ticketsite, "livepocket_listing_page", fail)
    assert worker.run_livepocket_sweep() == {}


def test_sweep_continues_when_db_reads_fail(sweep, fake_sb):
    fake_sb.get_ticket_page_rows.side_effect = RuntimeError("DB")
    fake_sb.get_last_seen_by_prefix.side_effect = RuntimeError("DB")

    report, _, _ = sweep([[sweep_card("hit0", day(1))]])

    assert target_names(report) == ["hit0"]
    assert len(report["warnings"]) == 2


def test_sweep_guard_swallows_exceptions(monkeypatch, capsys):
    monkeypatch.setattr(worker, "run_livepocket_sweep", MagicMock(side_effect=RuntimeError("壊れた")))
    worker._run_livepocket_sweep_guarded()
    assert "⚠" in capsys.readouterr().out


# --- 案G PR1: ノイズ除外と計測(LP-SWEEP-DATE-DESIGN.md §3.3・§3.4・§7) ---

def test_sweep_noise_defaults():
    assert worker.LP_SWEEP_MAX_SPAN_DAYS == 3
    assert worker.LP_SWEEP_PROBE_PAGES == (25, 30, 40, 50)
    assert "コラボカフェ" in worker._LP_SWEEP_NON_SHOW_WORDS
    assert "キャンペーン" in worker._LP_SWEEP_NON_SHOW_WORDS


@pytest.mark.parametrize("raw, expected", [
    ("25,30,40,50", (25, 30, 40, 50)),
    (" 25 , 30 ", (25, 30)),
    ("", ()),
    ("25,x,0,30", (25, 30)),
])
def test_sweep_probe_pages_setting(raw, expected):
    assert worker._lp_sweep_probe_pages(raw) == expected


def test_sweep_excludes_ended_events_by_end_date(sweep):
    pages = [[sweep_card("hit_ended_range", day(-3), end=day(-1)),
              sweep_card("hit_ended_single", day(-1)),
              sweep_card("hit_ongoing", day(-2), end=day()),
              sweep_card("hit0", day(1))]]

    report, _, _ = sweep(pages)

    assert target_names(report) == ["hit_ongoing", "hit0"]
    assert report["excluded"]["ended"] == 2
    # 既存の「範囲内で開催日が今日より前」の数え方は変えない
    assert report["past"] == 3
    # 開催済みは日付で客観的に決まるので、誤除外の監査には出さない
    assert report["audit"] == []


@pytest.mark.parametrize("start, end, kept", [
    (1, None, True),
    (1, 3, True),     # 3日間(両端を含む)は残す
    (1, 4, False),    # 4日間は長期の催し
    (-30, 10, False),  # 開始日が過去で終了日が先の、一覧の先頭に並ぶ催し
])
def test_sweep_excludes_long_running_events(sweep, start, end, kept):
    card = sweep_card("hit_event", day(start), end=day(end) if end is not None else None)

    report, _, _ = sweep([[card]])

    assert target_names(report) == (["hit_event"] if kept else [])
    assert report["excluded"]["long_span"] == (0 if kept else 1)
    assert len(report["audit"]) == (0 if kept else 1)


def test_sweep_long_span_threshold_is_adjustable(monkeypatch, sweep):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_SPAN_DAYS", 5)
    report, _, _ = sweep([[sweep_card("hit_fes", day(1), end=day(5)),
                           sweep_card("hit_long", day(1), end=day(6))]])
    assert target_names(report) == ["hit_fes"]
    assert report["excluded"]["long_span"] == 1


def test_sweep_excludes_non_show_words(sweep):
    pages = [[sweep_card("hit コラボカフェ 第2弾", day(1)),
              sweep_card("hit 来場キャンペーン", day(2)),
              sweep_card("hit ワンマンライブ", day(2))]]

    report, _, _ = sweep(pages)

    assert target_names(report) == ["hit ワンマンライブ"]
    assert report["excluded"]["non_show"] == 2


def test_sweep_audit_lists_long_span_and_non_show_cards(sweep):
    pages = [[sweep_card("hit_cafe_long", day(-10), end=day(20)),
              sweep_card("hit コラボカフェ", day(1)),
              # 登録グループに当たらないカードは、除外が無くても対象にならないので監査に出さない
              sweep_card("miss コラボカフェ", day(1)),
              sweep_card("hit0", day(2))]]

    report, _, _ = sweep(pages)

    assert report["audit"] == [
        {"reason": "long_span", "detail": "31日間", "dates": f"{day(-10)}〜{day(20)}",
         "name": "hit_cafe_long", "url": lp_url("hit_cafe_long")},
        {"reason": "non_show", "detail": "コラボカフェ", "dates": day(1),
         "name": "hit コラボカフェ", "url": lp_url("hit コラボカフェ")},
    ]
    assert report["excluded"]["no_group"] == 1


def test_sweep_audit_and_new_counts_appear_in_log_and_summary(sweep, monkeypatch, tmp_path, capsys):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    pages = [[sweep_card("hit_old", day(-2)),
              sweep_card("hit_cafe_long", day(-10), end=day(20)),
              sweep_card("hit コラボカフェ", day(1)),
              sweep_card("hit0", day(2))]]

    sweep(pages)

    text = path.read_text(encoding="utf-8")
    assert "| 開催済み | 1 |" in text
    assert "| 長期の催し | 1 |" in text
    assert "| 非公演の語 | 1 |" in text
    assert "誤除外の監査" in text
    assert (f"| 長期の催し: 31日間 | {day(-10)}〜{day(20)} | hit_cafe_long | "
            f"{lp_url('hit_cafe_long')} |") in text
    assert f"| 非公演の語: コラボカフェ | {day(1)} | hit コラボカフェ | {lp_url('hit コラボカフェ')} |" in text
    out = capsys.readouterr().out
    assert "開催済み 1 件" in out
    assert f"{day(-10)}〜{day(20)} hit_cafe_long {lp_url('hit_cafe_long')}" in out
    assert f"{day(1)} hit コラボカフェ {lp_url('hit コラボカフェ')}" in out


def test_sweep_warns_when_date_order_breaks(monkeypatch, sweep, tmp_path, capsys):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 3)
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    pages = [[sweep_card("hit0", day(1)), sweep_card("hit1", day(3))],
             [sweep_card("hit2", day(2)), sweep_card("hit3", day(4))],
             [sweep_card("hit4", day(4))]]

    report, requested, _ = sweep(pages)

    # この PR ではジャンプが無いので、警告だけで読み方は変えない
    assert requested == [1, 2, 3]
    assert target_names(report) == ["hit0", "hit1", "hit2", "hit3", "hit4"]
    assert report["order_breaks"] == [{"prev_page": 1, "prev_last": day(3), "page": 2, "first": day(2)}]
    assert any("開催日順が崩れています" in w for w in report["warnings"])
    assert "⚠ 開催日順が崩れています" in capsys.readouterr().out
    text = path.read_text(encoding="utf-8")
    assert "開催日順の崩れ: 1 か所" in text
    assert "⚠ 開催日順が崩れています" in text


def test_sweep_date_order_compares_across_pages_without_dates(monkeypatch, sweep):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 4)
    pages = [[sweep_card("hit0", day(2))],
             [sweep_card("hit1", day(2))],         # 同じ日付が続くのは崩れではない
             [sweep_card("hit_nodate", None)],      # 開催日の無いページは飛ばして比べる
             [sweep_card("hit2", day(1))]]

    report, _, _ = sweep(pages)

    assert report["order_breaks"] == [{"prev_page": 2, "prev_last": day(2), "page": 4, "first": day(1)}]


def test_sweep_reports_density(monkeypatch, sweep, capsys):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 1)
    pages = [[sweep_card("hit_past", day(-1)),
              sweep_card("hit_a", day()), sweep_card("hit_b", day()),
              sweep_card("hit_c", day(1)), sweep_card("hit_d", day(1)),
              sweep_card("hit_e", day(1)), sweep_card("hit_f", day(1)),
              sweep_card("hit_g", day(2))]]

    report, _, _ = sweep(pages)

    # 今日より前と、途中までの可能性がある最後の日(+2日)は数えない
    assert report["density"] == {"cards": 6, "days": 2, "per_day": 3.0, "through": day(2)}
    assert "1日あたり 3.0 件" in capsys.readouterr().out


def test_sweep_density_is_none_without_enough_days(sweep):
    report, _, _ = sweep([[sweep_card("hit0", day())]])
    assert report["density"] is None


def probe_pages(read, probes):
    """1〜len(read) ページは read、それより後は {ページ番号: 中身} の probes を返す一覧。"""
    last = max(probes) if probes else len(read)
    return [read[p - 1] if p <= len(read) else probes.get(p, "no_cards") for p in range(1, last + 1)]


def test_sweep_probe_reads_extra_pages_but_never_adds_targets(monkeypatch, sweep):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 2)
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", (25, 30))
    pages = probe_pages(
        [[sweep_card("hit0", day(1))], [sweep_card("hit1", day(2))]],
        # 範囲内の日付で登録グループにも当たるが、探り読みなので対象には入れない
        {25: [sweep_card("hit_probe25a", day(5)), sweep_card("hit_probe25b", day(6))],
         30: [sweep_card("hit_probe30", day(8))]})

    report, requested, ingest = sweep(pages)

    assert requested == [1, 2, 25, 30]
    assert target_names(report) == ["hit0", "hit1"]
    assert [c["name"] for c in report["cards"]] == ["hit0", "hit1"]
    assert report["probes"] == [
        {"page": 25, "cards": 2, "first": day(5), "last": day(6), "problem": None},
        {"page": 30, "cards": 1, "first": day(8), "last": day(8), "problem": None},
    ]
    # 読んだページ数・終了の理由・密度は通常の読み取りだけで決まる
    assert [p["page"] for p in report["pages"]] == [1, 2]
    assert report["stop"] == "max_pages"
    assert report["interrupted"] is False
    assert report["density"]["through"] == day(2)
    ingest.assert_not_called()


def test_sweep_probe_stops_at_first_problem(monkeypatch, sweep):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 1)
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", (25, 30, 40))
    pages = probe_pages([[sweep_card("hit0", day(1))]], {25: "no_cards", 30: [sweep_card("hit_x", day(9))]})

    report, requested, _ = sweep(pages)

    assert requested == [1, 25]
    assert report["probes"] == [{"page": 25, "cards": 0, "first": None, "last": None, "problem": "no_cards"}]
    assert report["stop"] == "max_pages"
    assert report["interrupted"] is False


def test_sweep_probe_survives_exception(monkeypatch, sweep):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 1)
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", (25,))
    pages = probe_pages([[sweep_card("hit0", day(1))]], {25: RuntimeError("壊れた")})

    report, _, _ = sweep(pages)

    assert report["probes"][0]["problem"] == "exception"
    assert target_names(report) == ["hit0"]
    assert report["interrupted"] is False


def test_sweep_probe_skips_pages_already_read(monkeypatch, sweep):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 3)
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", (2, 25))
    pages = probe_pages([[sweep_card(f"hit{i}", day(1))] for i in range(3)],
                        {25: [sweep_card("hit_probe", day(9))]})

    report, requested, _ = sweep(pages)

    assert requested == [1, 2, 3, 25]
    assert [p["page"] for p in report["probes"]] == [25]


def test_sweep_probe_is_skipped_after_interruption(monkeypatch, sweep, capsys):
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", (25,))

    report, requested, _ = sweep([[sweep_card("hit0", day(1))], "waf"])

    assert requested == [1, 2]
    assert report["probes"] == []
    assert "途中停止したので読まなかった" in capsys.readouterr().out


def test_sweep_probe_appears_in_summary(monkeypatch, sweep, tmp_path):
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 1)
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", (25, 30))
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    pages = probe_pages([[sweep_card("hit0", day(1))]],
                        {25: [sweep_card("hit_p", day(7)), sweep_card("hit_q", day(8))], 30: "waf"})

    sweep(pages)

    text = path.read_text(encoding="utf-8")
    assert "探り読み" in text
    assert f"| 25 | 2 | {day(7)} | {day(8)} | - |" in text
    assert "| 30 | 0 | 不明 | 不明 | 途中停止: WAF の認証ページ |" in text


def test_sweep_trial_with_noise_and_probe_never_writes_db(monkeypatch, sweep, fake_sb):
    """試運転の性質: 新しい除外と探り読みが動いても、DB にも状態にも書き込まない。"""
    monkeypatch.setattr(worker, "LP_SWEEP_MAX_PAGES", 2)
    monkeypatch.setattr(worker, "LP_SWEEP_PROBE_PAGES", (25, 30))
    pages = probe_pages(
        [[sweep_card("hit_old", day(-2)), sweep_card("hit_long", day(-10), end=day(20)),
          sweep_card("hit コラボカフェ", day(1)), sweep_card("hit0", day(3))],
         [sweep_card("hit1", day(2))]],   # 開催日順の崩れも起こす
        {25: [sweep_card("hit_probe", day(5))], 30: "no_cards"})
    last = {cache_key("hit0"): day(-5)}

    report, requested, ingest = sweep(pages, last=last)

    assert requested == [1, 2, 25, 30]
    assert target_names(report) == ["hit0", "hit1"]
    assert report["order_breaks"]
    ingest.assert_not_called()
    worker.ticketsite.fetch_event.assert_not_called()
    fake_sb.set_last_seen.assert_not_called()
    fake_sb.upsert_auto_events.assert_not_called()
    called = {c[0] for c in fake_sb.method_calls}
    assert called == {"get_ticket_page_rows", "get_last_seen_by_prefix"}
    assert not [n for n in called if n.startswith(("set_", "upsert", "insert", "delete", "update"))]


# --- main() への組み込み ---

MAIN_STEPS = ("_run_venues_guarded", "run_sites", "run_timetree", "run_gcal", "run_eplus", "run_curated",
              "run_yahoo_rt", "run_livepocket", "run_livepocket_sweep", "run_tiget", "run_release",
              "_reconcile_placeholder_guarded", "refresh_sale_info", "fix_ticket_urls",
              "cleanup_orphan_rows", "share_sale_info")


@pytest.fixture
def main_steps(monkeypatch):
    for name in ("X_ONLY", "DISCOVERY_ONLY", "QUICK_LISTING", "SITE_ONLY", "GCAL_ONLY", "LIVEPOCKET_ONLY",
                 "TIGET_ONLY", "RELEASE_ONLY", "SALE_ONLY", "LISTING_CHECK", "VENUE_ONLY",
                 "ENABLE_X_INGEST"):
        monkeypatch.delenv(name, raising=False)
    order = MagicMock()
    for name in MAIN_STEPS:
        monkeypatch.setattr(worker, name, getattr(order, name))
    monkeypatch.setattr(worker, "sb", MagicMock())
    return order


def called_names(order):
    return [c[0] for c in order.method_calls]


def test_full_run_sweeps_right_after_run_livepocket(main_steps):
    assert worker.main() == 0
    called = called_names(main_steps)
    i = called.index("run_livepocket")
    assert called[i + 1:i + 3] == ["run_livepocket_sweep", "run_tiget"]


def test_sweep_failure_does_not_stop_full_run(main_steps, capsys):
    main_steps.run_livepocket_sweep.side_effect = RuntimeError("壊れた")
    assert worker.main() == 0
    assert called_names(main_steps)[-1] == "cleanup_orphan_rows"
    assert "run_tiget" in called_names(main_steps)
    assert "⚠" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["QUICK_LISTING", "LIVEPOCKET_ONLY", "DISCOVERY_ONLY"])
def test_light_modes_do_not_sweep(main_steps, monkeypatch, flag):
    monkeypatch.setenv(flag, "true")
    assert worker.main() == 0
    assert "run_livepocket_sweep" not in called_names(main_steps)
