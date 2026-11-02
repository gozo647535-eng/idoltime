"""公演ページ(案内先URL)を保存前に本文と照合する処理のテスト。

ネットワークには触れない。_detail_page_text を差し替える。
"""
import os
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
