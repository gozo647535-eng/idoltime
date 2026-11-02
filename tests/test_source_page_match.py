"""公演ページ(案内先URL)を保存前に本文と照合する処理のテスト。

ネットワークには触れない。_detail_page_text を差し替える。
"""
import os
import urllib.parse
from types import SimpleNamespace

import pytest

# worker / sb は import 時に環境変数を読むので、実環境の設定を持ち込まない
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(_key, None)

import sites  # noqa: E402
import worker  # noqa: E402

SITE = "https://marmi.tokyo/"
NAME = "Lost in Wonderland リリース記念フリーライブ"
DETAIL = "https://marmi.tokyo/live_information/detail/45720"
OTHER = "https://marmi.tokyo/live_information/detail/50000"
# 実例: 別の日の公演のページだが、曲名として公演名の一部を含む
WRONG_PAGE = "2026年10月11日(日) ららぽーと湘南平塚 ミニライブ\n披露曲: Lost in Wonderland"


def event(**overrides):
    fields = {"event_name": NAME, "event_datetime": "2026-11-08 13:00"}
    fields.update(overrides)
    return sites.SiteEvent(**fields)


@pytest.fixture
def pages(monkeypatch):
    """URL → 本文。読んだURLは reads に残る。載っていないURLは読めないページ(None)。"""
    bodies, reads = {}, []

    def fake(url):
        reads.append(url)
        return bodies.get(url)

    monkeypatch.setattr(worker, "_detail_page_text", fake)
    monkeypatch.setattr(sites, "DATED_ANCHORS", {})
    monkeypatch.setattr(sites, "PAGE_TITLES", {})
    return SimpleNamespace(bodies=bodies, reads=reads)


# --- 照合そのもの ---

def test_other_date_with_partial_name_is_rejected(pages, capsys):
    pages.bodies[DETAIL] = WRONG_PAGE
    assert worker._source_page_matches(event(), DETAIL) is False
    out = capsys.readouterr().out
    assert f"案内先を照合で除外: {DETAIL} (公演日 2026-11-08 がページに無い)" in out


def test_page_with_event_date_matches(pages, capsys):
    pages.bodies[DETAIL] = "2026年11月8日(日) 東京\nフリーライブ"
    assert worker._source_page_matches(event(), DETAIL) is True
    assert "除外" not in capsys.readouterr().out


@pytest.mark.parametrize("written", ["11月8日(日)", "11/8(日)", "11.8 SUN"])
def test_yearless_event_date_beside_dated_post_matches(pages, written):
    # 掲載日や受付期間だけが年つきでも、年なしで書かれた公演日を見落とさない
    pages.bodies[DETAIL] = f"掲載日 2026.10.01\n公演日 {written}\n受付期間 2026年10月18日 10:00"
    assert worker._source_page_matches(event(), DETAIL) is True


def test_same_day_of_another_year_is_rejected(pages):
    pages.bodies[DETAIL] = "2025年11月8日(土) 昨年の公演"
    assert worker._source_page_matches(event(), DETAIL) is False


def test_yearless_date_in_next_january_matches(pages):
    pages.bodies[DETAIL] = "1月10日(日) 新春ライブ"
    assert worker._source_page_matches(event(event_datetime="2027-01-10"), DETAIL) is True


def test_no_date_but_long_name_overlap_matches(pages):
    pages.bodies[DETAIL] = "Lost in Wonderland リリース記念 開催決定"
    assert worker._source_page_matches(event(), DETAIL) is True


def test_no_date_but_short_name_contained_matches(pages):
    pages.bodies[DETAIL] = "定期公演 ABC の案内"
    assert worker._source_page_matches(event(event_name="ABC"), DETAIL) is True


def test_no_date_and_no_overlap_is_rejected(pages, capsys):
    pages.bodies[DETAIL] = "新しいグッズのお知らせ"
    assert worker._source_page_matches(event(), DETAIL) is False
    assert f"案内先を照合で除外: {DETAIL}" in capsys.readouterr().out


def test_no_date_and_short_overlap_is_rejected(pages):
    # 5文字の重なりでは足りない
    pages.bodies[DETAIL] = "Losti の話"
    assert worker._source_page_matches(event(event_name="Lost in Wonderland"), DETAIL) is False


def test_no_date_and_empty_name_is_rejected(pages):
    pages.bodies[DETAIL] = "新しいグッズのお知らせ"
    assert worker._source_page_matches(event(event_name=""), DETAIL) is False


@pytest.mark.parametrize("body", [None, "", " \n "])
def test_unreadable_page_matches(pages, body):
    pages.bodies[DETAIL] = body
    assert worker._source_page_matches(event(), DETAIL) is True


def test_fetch_error_matches(monkeypatch):
    def boom(_url):
        raise OSError("network down")

    monkeypatch.setattr(worker, "_detail_page_text", boom)
    assert worker._source_page_matches(event(), DETAIL) is True


@pytest.mark.parametrize("raw", [None, "未定"])
def test_unknown_event_date_matches_without_reading(pages, raw):
    pages.bodies[DETAIL] = WRONG_PAGE
    assert worker._source_page_matches(event(event_datetime=raw), DETAIL) is True
    assert pages.reads == []


# --- 候補の選択 ---

def test_wrong_candidate_falls_back_to_top(pages):
    pages.bodies[DETAIL] = WRONG_PAGE
    url = worker._site_event_url(event(), SITE, anchors=[(NAME, DETAIL)])
    assert url == SITE


def test_right_candidate_is_used(pages):
    pages.bodies[DETAIL] = "2026年11月8日 Lost in Wonderland"
    url = worker._site_event_url(event(), SITE, anchors=[(NAME, DETAIL)])
    assert url == DETAIL


def test_unreadable_candidate_is_used(pages):
    url = worker._site_event_url(event(), SITE, anchors=[(NAME, DETAIL)])
    assert url == DETAIL


def test_source_url_is_checked_and_next_candidate_taken(pages):
    pages.bodies[DETAIL] = WRONG_PAGE
    pages.bodies[OTHER] = "2026/11/08 東京"
    url = worker._site_event_url(event(source_url=DETAIL), SITE, anchors=[(NAME, OTHER)])
    assert url == OTHER


def test_source_url_of_other_host_is_not_read(pages):
    url = worker._site_event_url(event(source_url="https://other.example/e/1"), SITE)
    assert url == SITE
    assert pages.reads == []


def test_dated_anchor_is_checked(pages, monkeypatch):
    pages.bodies[DETAIL] = WRONG_PAGE
    monkeypatch.setitem(sites.DATED_ANCHORS, SITE, [("2026-11-08", NAME, DETAIL, True)])
    assert worker._site_event_url(event(), SITE) == SITE


def test_page_title_is_checked(pages, monkeypatch):
    pages.bodies[DETAIL] = WRONG_PAGE
    monkeypatch.setitem(sites.PAGE_TITLES, SITE, [(NAME, DETAIL)])
    assert worker._site_event_url(event(), SITE) == SITE


def test_page_tag_is_checked(pages):
    pages.bodies[DETAIL] = WRONG_PAGE
    assert worker._site_event_url(event(source_page="p1"), SITE, [("P1", DETAIL)]) == SITE
    pages.bodies[DETAIL] = "11月8日 東京"
    assert worker._site_event_url(event(source_page="p1"), SITE, [("P1", DETAIL)]) == DETAIL


def test_top_page_is_not_checked(pages):
    pages.bodies[SITE] = WRONG_PAGE
    assert worker._site_event_url(event(source_page="P1"), SITE, [("P1", SITE)]) == SITE
    assert pages.reads == []


def test_later_candidates_are_not_read_after_a_match(pages, monkeypatch):
    pages.bodies[DETAIL] = "11月8日 東京"
    monkeypatch.setitem(sites.DATED_ANCHORS, SITE, [("2026-11-08", NAME, OTHER, True)])
    assert worker._site_event_url(event(), SITE, anchors=[(NAME, DETAIL)]) == DETAIL
    assert pages.reads == [DETAIL]


def test_url_rejected_once_is_not_checked_again(pages, monkeypatch, capsys):
    pages.bodies[DETAIL] = WRONG_PAGE
    monkeypatch.setitem(sites.DATED_ANCHORS, SITE, [("2026-11-08", NAME, DETAIL, True)])
    assert worker._site_event_url(event(), SITE, anchors=[(NAME, DETAIL)]) == SITE
    assert pages.reads == [DETAIL]
    assert capsys.readouterr().out.count("案内先を照合で除外") == 1


# --- 実際のキャッシュ ---

def test_same_url_is_fetched_once_across_events(monkeypatch):
    calls = []

    def fake_get(url):
        calls.append(url)
        return f"<html><body><article>{WRONG_PAGE}{'。' * 200}</article></body></html>"

    monkeypatch.setattr(worker, "_detail_cache", {})
    monkeypatch.setattr(sites, "_get", fake_get)
    # 本文が短いと判定されてもブラウザを開かない
    monkeypatch.setattr(sites, "_fetch_subpage", fake_get)
    monkeypatch.setattr(sites, "DATED_ANCHORS", {})
    monkeypatch.setattr(sites, "PAGE_TITLES", {})
    for day in ("2026-11-08", "2026-11-14", "2026-11-15", "2026-11-21", "2026-11-22"):
        url = worker._site_event_url(event(event_datetime=day), SITE, anchors=[(NAME, DETAIL)])
        assert url == SITE
    assert calls == [DETAIL]


# --- 三値の判定 ---

def test_verdict_date_present_is_match(pages):
    pages.bodies[DETAIL] = "2026年11月8日(日) 東京"
    assert worker._source_page_verdict(event(), DETAIL) == "match"


def test_verdict_other_date_is_mismatch(pages):
    pages.bodies[DETAIL] = WRONG_PAGE
    assert worker._source_page_verdict(event(), DETAIL) == "mismatch"


def test_verdict_no_date_with_name_overlap_is_match(pages):
    pages.bodies[DETAIL] = "Lost in Wonderland リリース記念 開催決定"
    assert worker._source_page_verdict(event(), DETAIL) == "match"


def test_verdict_no_date_without_name_overlap_is_mismatch(pages):
    pages.bodies[DETAIL] = "新しいグッズのお知らせ"
    assert worker._source_page_verdict(event(), DETAIL) == "mismatch"


@pytest.mark.parametrize("body", [None, "", " \n "])
def test_verdict_unreadable_page_is_unknown_and_still_accepted(pages, body):
    pages.bodies[DETAIL] = body
    assert worker._source_page_verdict(event(), DETAIL) == "unknown"
    assert worker._source_page_matches(event(), DETAIL) is True


def test_verdict_fetch_error_is_unknown(monkeypatch):
    def boom(_url):
        raise OSError("network down")

    monkeypatch.setattr(worker, "_detail_page_text", boom)
    assert worker._source_page_verdict(event(), DETAIL) == "unknown"


def test_verdict_unknown_event_date_is_unknown_without_reading(pages):
    pages.bodies[DETAIL] = WRONG_PAGE
    assert worker._source_page_verdict(event(event_datetime=None), DETAIL) == "unknown"
    assert pages.reads == []


# --- 保存済みの行の付け直し ---

SLUG = "marmi"
LIST_URL = "https://marmi.tokyo/schedule"
RIGHT_PAGE = "2026年11月8日(日) Lost in Wonderland フリーライブ"


def row(tweet_url, tweet_id="site:1", **overrides):
    fields = {"tweet_id": tweet_id, "idol_slug": SLUG, "event_name": NAME,
              "event_datetime": "2026-11-08", "tweet_url": tweet_url}
    fields.update(overrides)
    return fields


def is_detail(r):
    # refresh_site_details の is_detail と同じ条件
    url = r.get("tweet_url") or ""
    return (urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(SITE).netloc
            and url.rstrip("/") != SITE.rstrip("/"))


@pytest.fixture
def relink(pages, monkeypatch):
    """サイトの読み直しと書き込みを差し替えて _relink_site_rows を呼ぶ。"""
    state = SimpleNamespace(anchors=[], fetches=[], patches=[], seen_marks=[],
                            fail_fetch=False, fail_patch=False, pages=pages)

    def fetch(site):
        state.fetches.append(site)
        if state.fail_fetch:
            raise OSError("site down")
        return None, "", [], [], [], state.anchors

    def patch(tweet_id, body):
        if state.fail_patch:
            raise OSError("db down")
        state.patches.append((tweet_id, body))

    monkeypatch.setattr(sites, "fetch_site_content", fetch)
    monkeypatch.setattr(worker.sb, "patch_auto_event", patch)
    monkeypatch.setattr(worker.sb, "set_last_seen_many", state.seen_marks.extend)

    def run(rows, seen=None):
        return worker._relink_site_rows(rows, {SLUG: SITE}, None, seen or {}, "2026-11-01", is_detail)

    state.run = run
    return state


def test_mismatched_detail_row_moves_to_right_page(relink, capsys):
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    relink.pages.bodies[OTHER] = RIGHT_PAGE
    relink.anchors = [(NAME, OTHER)]
    r = row(DETAIL)
    assert relink.run([r]) == {"site:1"}
    assert relink.patches == [("site:1", {"tweet_url": OTHER})]
    assert r["tweet_url"] == OTHER
    out = capsys.readouterr().out
    assert f"{DETAIL} → {OTHER}" in out
    assert "不一致 1 件を付け替えた(うちトップへ 0 件)" in out


def test_mismatched_detail_row_without_candidate_falls_to_top(relink, capsys):
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    r = row(DETAIL)
    assert relink.run([r]) == {"site:1"}
    assert relink.patches == [("site:1", {"tweet_url": SITE})]
    assert r["tweet_url"] == SITE
    assert "不一致 1 件を付け替えた(うちトップへ 1 件)" in capsys.readouterr().out


def test_mismatched_detail_row_rejected_candidate_falls_to_top(relink):
    # 候補があっても同じ誤ったページなら付け替え先にしない
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    relink.anchors = [(NAME, DETAIL)]
    r = row(DETAIL)
    relink.run([r])
    assert r["tweet_url"] == SITE


def test_mismatched_detail_row_does_not_move_to_list_page(relink):
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    relink.pages.bodies[LIST_URL] = RIGHT_PAGE
    relink.anchors = [(NAME, LIST_URL)]
    r = row(DETAIL)
    relink.run([r])
    assert r["tweet_url"] == SITE


def test_matching_detail_row_is_not_touched(relink):
    relink.pages.bodies[DETAIL] = RIGHT_PAGE
    relink.pages.bodies[OTHER] = RIGHT_PAGE
    relink.anchors = [(NAME, OTHER)]
    r = row(DETAIL)
    assert relink.run([r]) == set()
    assert relink.patches == []
    assert r["tweet_url"] == DETAIL


def test_unreadable_detail_row_is_not_touched(relink):
    r = row(DETAIL)  # 本文が取れない
    assert relink.run([r]) == set()
    assert relink.patches == []
    assert r["tweet_url"] == DETAIL


def test_detail_rows_without_mismatch_skip_the_site_fetch(relink):
    relink.pages.bodies[DETAIL] = RIGHT_PAGE
    relink.run([row(DETAIL)])
    assert relink.fetches == []
    # 確かめたサイトとして記録され、次の順番まで回ってこない
    assert [k for k, _v in relink.seen_marks] == [f"relink:{SLUG}"]


def test_mismatched_row_stays_when_site_fetch_fails(relink):
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    relink.fail_fetch = True
    r = row(DETAIL)
    assert relink.run([r]) == set()
    assert r["tweet_url"] == DETAIL
    assert relink.seen_marks == []  # 次の実行でやり直す


def test_failed_write_keeps_row_and_is_not_counted(relink, capsys):
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    relink.fail_patch = True
    r = row(DETAIL)
    assert relink.run([r]) == set()
    assert r["tweet_url"] == DETAIL
    assert "不一致 0 件" in capsys.readouterr().out


@pytest.mark.parametrize("current", [SITE, LIST_URL])
def test_top_and_list_rows_are_still_relinked(relink, current, capsys):
    relink.pages.bodies[DETAIL] = RIGHT_PAGE
    relink.anchors = [(NAME, DETAIL)]
    r = row(current)
    assert relink.run([r]) == {"site:1"}
    assert r["tweet_url"] == DETAIL
    out = capsys.readouterr().out
    assert "1 件の案内先を公演ページにした" in out
    assert "不一致 0 件" in out


def test_list_row_is_not_moved_to_top_without_candidate(relink):
    # トップへの付け替えは不一致の公演ページの行に限る。一覧の本文が別の日でも同じ
    relink.pages.bodies[LIST_URL] = WRONG_PAGE
    r = row(LIST_URL)
    assert relink.run([r]) == set()
    assert relink.patches == []
    assert r["tweet_url"] == LIST_URL
    assert LIST_URL not in relink.pages.reads


def test_top_row_is_not_rewritten_to_list_page(relink):
    relink.pages.bodies[LIST_URL] = RIGHT_PAGE
    relink.anchors = [(NAME, LIST_URL)]
    r = row(SITE)
    assert relink.run([r]) == set()
    assert relink.patches == []


def test_recently_checked_site_is_skipped(relink):
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    r = row(DETAIL)
    assert relink.run([r], seen={f"relink:{SLUG}": "2026-11-02"}) == set()
    assert relink.pages.reads == []
    assert relink.fetches == []


def test_site_limit_applies_to_detail_rows(relink, monkeypatch):
    monkeypatch.setattr(worker, "DETAIL_RELINK_SITES", 1)
    relink.pages.bodies[DETAIL] = RIGHT_PAGE
    relink.pages.bodies[OTHER] = RIGHT_PAGE
    rows = [row(DETAIL, tweet_id="site:1"), row(OTHER, tweet_id="site:2", idol_slug="other")]
    worker._relink_site_rows(rows, {SLUG: SITE, "other": SITE}, None, {}, "2026-11-01", is_detail)
    assert relink.pages.reads == [DETAIL]


def test_refresh_reads_repaired_page_but_not_row_moved_to_top(relink, monkeypatch):
    # 付け替えた行だけ新しいページの中身を読み、トップに落ちた行は読まない
    relink.pages.bodies[DETAIL] = WRONG_PAGE
    relink.pages.bodies[OTHER] = "2026年11月14日 東京"
    relink.anchors = [(NAME, OTHER)]
    repaired = row(DETAIL, tweet_id="site:1", event_datetime="2026-11-14")
    moved = row(DETAIL, tweet_id="site:2")
    enriched = []
    monkeypatch.setattr(worker, "build_site_watch_list",
                        lambda include_skipped=False: [{"idol_slug": SLUG, "site_url": SITE}])
    monkeypatch.setattr(worker.sb, "get_site_rows_for_detail", lambda _today: [repaired, moved])
    monkeypatch.setattr(worker.sb, "get_last_seen_by_prefix", lambda _prefix: {})
    monkeypatch.setattr(worker, "enrich_site_row_from_detail",
                        lambda r, _site: enriched.append(r["tweet_id"]) or r)
    monkeypatch.setattr(worker, "enrich_row_with_ticketdive", lambda r: r)
    monkeypatch.setattr(worker, "build_idol_name_list", lambda: [])
    worker.refresh_site_details()
    assert repaired["tweet_url"] == OTHER
    assert moved["tweet_url"] == SITE
    assert enriched == ["site:1"]
