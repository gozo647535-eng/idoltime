"""会場ベース収集 Phase 1(試運転)のテスト。

ネットワークにも Supabase にも触れない。取得は sites._get を差し替える。
fixture は実サイトの HTML(2026-11-03 取得)から行を選んだ抜粋で、構造は実物のまま。
"""
import json
import os
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

# worker / sb は import 時に環境変数を読むので、実環境の設定を持ち込まない
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
             "VENUE_ONLY", "VENUE_IDS", "GITHUB_STEP_SUMMARY"):
    os.environ.pop(_key, None)

import lineup  # noqa: E402
import sites  # noqa: E402
import venues  # noqa: E402
import worker  # noqa: E402

TODAY = "2026-11-03"
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "venues")
EDWARD_URL = "https://www.edward.co.jp/theater/"
EBEANS_URL = "https://www.e-beans.jp/eventcalendars/%e3%83%aa%e3%82%b9%e3%83%88/"
RENSA_NOV = "http://www.rensa.jp/schedule/?index=2026/11"
IDX = lineup.build_index([
    {"name": "登録グループ", "idol_slug": "artist:登録グループ"},
    {"name": "BAND", "idol_slug": "artist:BAND"},
])
VENUE = {"id": "rensa", "name": "仙台Rensa", "status": "shadow", "filter": {}}


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def soup(name):
    return BeautifulSoup(fixture(name), "html.parser")


@pytest.fixture
def registry():
    return {v["id"]: v for v in venues.load_registry()}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(venues, "REQUEST_INTERVAL", 0)


def valid_venue(**overrides):
    v = {
        "id": "test-1", "name": "テスト会場", "aliases": [], "prefecture": "宮城県", "city": "仙台市",
        "official_url": "https://example.com/", "schedule_urls": ["https://example.com/s"],
        "method": "selector", "selector": {"item": ".x"}, "status": "shadow",
    }
    v.update(overrides)
    return v


def event(**overrides):
    ev = {"title": "公演", "date": "2026-11-10", "start": None, "artist": None, "body": None,
          "item_text": "", "link": None, "ext_id": None}
    ev.update(overrides)
    return ev


# --- レジストリ ---

def test_registry_lists_all_candidates_with_expected_status(registry):
    assert {k: v["status"] for k, v in registry.items()} == {
        "ebeans": "shadow", "rensa": "shadow", "darwin": "shadow", "daremo": "shadow",
        "gigs": "off", "xebio": "off", "junkbox": "none", "tsutsujigaoka": "none",
    }
    assert "2024-02-29" in registry["junkbox"]["notes"]


def test_select_targets_uses_only_active_and_honours_ids(registry):
    reg = list(registry.values())
    assert {v["id"] for v in venues.select_targets(reg)} == {"ebeans", "rensa", "darwin", "daremo"}
    assert [v["id"] for v in venues.select_targets(reg, ["rensa", "gigs", "nope"])] == ["rensa"]


def test_invalid_venue_is_skipped_with_warning(tmp_path, capsys):
    path = tmp_path / "venues.json"
    path.write_text(json.dumps([
        valid_venue(),
        valid_venue(id="Bad:ID"),
        valid_venue(id="no-selector", selector={"item": "[[["}),
        valid_venue(id="test-1", name="id の重複"),
        "会場ではない",
    ], ensure_ascii=False), encoding="utf-8")
    got = venues.load_registry(str(path))
    assert [v["name"] for v in got] == ["テスト会場"]
    out = capsys.readouterr().out
    assert "⚠" in out and "Bad:ID" in out and "no-selector" in out and "重複" in out


def test_jsonld_config_is_validated():
    good = {"id_pattern": "x-(\\d+)",
            "category": {"item": "article", "id_class": "^post-(\\d+)$", "allow_class": "cat02"}}
    assert not venues.validate_venue(valid_venue(method="jsonld", selector=None, jsonld=good))
    assert not venues.validate_venue(valid_venue(method="jsonld", selector=None))
    for bad in (
        "文字列",
        {"id_pattern": "グループなし"},
        {"id_pattern": "x-(\\d+)", "category": {"item": "article"}},
        {"id_pattern": "x-(\\d+)", "category": dict(good["category"], item="[[[")},
        {"category": good["category"]},   # Event 側の ID が無いと突き合わせられない
    ):
        assert venues.validate_venue(valid_venue(method="jsonld", selector=None, jsonld=bad)), bad


def test_new_selector_fields_are_validated():
    assert venues.validate_venue(valid_venue(selector={"item": ".x", "item_next": "[[["}))
    assert venues.validate_venue(valid_venue(selector={"item": ".x", "artist_strip": "("}))
    assert venues.validate_venue(valid_venue(selector={"item": ".x", "date_attr": 1}))


def test_broken_json_warns_and_returns_empty(tmp_path, capsys):
    path = tmp_path / "venues.json"
    path.write_text("[{", encoding="utf-8")
    assert venues.load_registry(str(path)) == []
    assert "⚠" in capsys.readouterr().out


def test_active_venue_requires_urls_but_off_does_not():
    assert venues.validate_venue(valid_venue(schedule_urls=[]))
    assert not venues.validate_venue(valid_venue(
        status="off", method="none", selector=None, official_url="", schedule_urls=[]))


def test_month_templates_expand_across_year_end(registry):
    assert venues.expand_schedule_urls(registry["rensa"], TODAY) == [
        (RENSA_NOV, 2026, 11),
        ("http://www.rensa.jp/schedule/?index=2026/12", 2026, 12),
        ("http://www.rensa.jp/schedule/?index=2027/01", 2027, 1),
        ("http://www.rensa.jp/schedule/?index=2027/02", 2027, 2),
    ]
    assert venues.expand_schedule_urls(registry["darwin"], TODAY) == [(EDWARD_URL, None, None)]


# --- 日付と時刻 ---

@pytest.mark.parametrize("text,year,month,expected", [
    ("2026年11月10日(火)", None, None, "2026-11-10"),
    ("2027.01.05", 2026, 11, "2027-01-05"),
    ("１１月１０日", None, None, "2026-11-10"),
    ("1/5", 2026, 12, "2027-01-05"),      # 12月のページの1月は翌年
    ("12/28", 2027, 1, "2026-12-28"),     # 1月のページの12月は前年
    ("1/5", None, None, "2027-01-05"),    # ページの年月が無ければ今日に近い年
    ("8/1", None, None, "2026-08-01"),    # 古い公演を来年に化けさせない
    ("13/45", None, None, None),
    ("18:00", None, None, None),
    ("", None, None, None),
])
def test_parse_date(text, year, month, expected):
    assert venues.parse_date(text, year, month, TODAY) == expected


def test_day_only_needs_month_page_and_explicit_permission():
    assert venues.parse_date("10(火)", 2026, 11, TODAY, allow_day_only=True) == "2026-11-10"
    assert venues.parse_date("10(火)", 2026, 11, TODAY) is None
    assert venues.parse_date("10(火)", None, None, TODAY, allow_day_only=True) is None


@pytest.mark.parametrize("text,expected", [
    ("OPEN 17:30 / START 18:00", ("17:30", "18:00")),
    ("開場17時30分 開演18時", ("17:30", "18:00")),
    ("OPEN・START\n17:30 / 18:00", ("17:30", "18:00")),
    ("OPEN/START 17:30/18:00", ("17:30", "18:00")),
    ("ＯＰＥＮ １７：３０", ("17:30", None)),   # 開場しか無ければ開演は空のまま
    ("18:00〜", (None, None)),                 # ラベルの無い時刻は使わない
    ("START 25:00", (None, None)),
    # 以下は実ページの表記。2部制は最初の回を使う
    ("OPEN/START\xa0\xa0①\xa012:00\xa0/\xa012:45\xa0②\xa016:30\xa0/\xa017:15", ("12:00", "12:45")),
    ("OPEN\n① 14:30　② 17:15\nSTART\n① 15:00　② 17:45", ("14:30", "15:00")),
    ("OPEN\n①12:45②16:15\nSTART\n①13:30②17:00", ("12:45", "13:30")),
    ("OPEN\n①11:30/②15:30\nSTART\n①12:00/②16:00", ("11:30", "12:00")),
    ("OPEN\n未定\nSTART\n17:30", (None, "17:30")),
    ("OPEN\n17:00\nSTART\n17;30", ("17:00", "17:30")),
])
def test_parse_times_never_uses_open_as_start(text, expected):
    assert venues.parse_times(text) == expected


# --- 抽出: イービーンズ(JSON-LD + 分類) ---

JUSFES = "エンドー音楽堂presents 『ジャスフェス2026』開催！！"


def ebeans_items(registry):
    return venues.extract_jsonld(soup("ebeans_list.html"), registry["ebeans"], EBEANS_URL)


def test_ebeans_keeps_only_live_category(registry):
    items = ebeans_items(registry)
    # JSON-LD の3件のうち cat04(物販)の 22988 が落ちる
    assert [i["ext_id"] for i in items] == ["23239", "22821"]


def test_ebeans_uses_jsonld_name_and_start_date_without_time(registry):
    ev = ebeans_items(registry)[0]
    assert ev["title"] == JUSFES
    assert ev["date"] == "2026-11-08"
    assert (ev["open"], ev["start"], ev["artist"]) == (None, None, None)
    assert ev["link"] == "https://www.e-beans.jp/eventcalendar/tribe_events-23239/"


def test_ebeans_event_without_category_element_is_dropped(registry):
    html = fixture("ebeans_list.html").replace("post-23239", "post-99999")
    items = venues.extract_jsonld(BeautifulSoup(html, "html.parser"), registry["ebeans"], EBEANS_URL)
    assert [i["ext_id"] for i in items] == ["22821"]


def test_ebeans_missing_category_elements_warn(registry, monkeypatch, capsys):
    html = fixture("ebeans_list.html").replace("<article", "<section").replace("</article", "</section")
    monkeypatch.setattr(sites, "_get", lambda url, timeout=20: html)
    events, warnings = venues.collect_venue(registry["ebeans"], venues.Fetcher(), TODAY)
    assert events == []
    assert any("抽出0件" in w for w in warnings)
    assert "分類の要素" in capsys.readouterr().out


def test_jsonld_without_category_keeps_all_events(registry):
    v = {"id": "x", "method": "jsonld", "jsonld": {"id_pattern": "tribe_events-(\\d+)"}}
    items = venues.extract_jsonld(soup("ebeans_list.html"), v, EBEANS_URL)
    assert [i["ext_id"] for i in items] == ["22988", "23239", "22821"]


def test_jsonld_skips_broken_json_and_other_types(registry):
    html = """<script type="application/ld+json">{壊れている</script>
    <script type="application/ld+json">{"@type":"Organization","name":"会社"}</script>
    <script type="application/ld+json">{"@type":"Event","name":"A &amp; B","startDate":"2026-11-10"}</script>
    """
    v = {"id": "x", "method": "jsonld"}
    items = venues.extract_jsonld(BeautifulSoup(html, "html.parser"), v, EBEANS_URL)
    assert [(i["title"], i["date"]) for i in items] == [("A & B", "2026-11-10")]


def test_ebeans_collect_reads_only_the_list_page(registry, monkeypatch):
    get = MagicMock(return_value=fixture("ebeans_list.html"))
    monkeypatch.setattr(sites, "_get", get)
    events, warnings = venues.collect_venue(registry["ebeans"], venues.Fetcher(), TODAY)
    assert get.call_count == 1
    assert get.call_args[0][0] == EBEANS_URL
    assert len(events) == 2 and warnings == []


def test_ebeans_event_has_no_start_but_can_be_judged(registry):
    ev = ebeans_items(registry)[0]
    got = worker.judge_venue_event(ev, registry["ebeans"], IDX, TODAY)
    assert got["result"] == "hold"   # 公演名だけでは判定材料が無い


# --- 抽出: Rensa(月別ページ) ---

def rensa_items(registry, year=2026, month=11):
    return venues.extract_items(soup("rensa_202611.html"), registry["rensa"], RENSA_NOV, year, month, TODAY)


def test_rensa_reads_date_from_name_attribute(registry):
    assert [i["date"] for i in rensa_items(registry)] == [
        "2026-11-01", "2026-11-05", "2026-11-11", "2026-11-19", "2026-11-27"]
    # ページの年月や文字の「05」ではなく、属性の YYYYMMDD が決め手
    assert rensa_items(registry, 2027, 1)[1]["date"] == "2026-11-05"


def test_rensa_date_falls_back_to_day_text_without_attribute(registry):
    html = """<div class="monthly_schedule_primaly"><div class="schedule_date"><a>05</a></div>
    <div class="schedule_artist"><h1>出演者</h1></div></div>"""
    items = venues.extract_items(BeautifulSoup(html, "html.parser"), registry["rensa"], RENSA_NOV, 2026, 12, TODAY)
    assert items[0]["date"] == "2026-12-05"


def test_rensa_pair_gives_title_artist_times_and_link(registry):
    ev = rensa_items(registry)[1]
    assert ev["title"] == "パジャマライブ"
    assert ev["artist"] == "NeW sTyle"
    assert (ev["open"], ev["start"]) == ("18:00", "18:30")
    assert ev["link"] == "http://www.rensa.jp/schedule_more/?index=3376"
    assert ev["ext_id"] == "3376"


def test_rensa_two_show_day_uses_first_times(registry):
    ev = rensa_items(registry)[2]
    assert (ev["open"], ev["start"]) == ("12:00", "12:45")


def test_rensa_makeup_note_in_artist_does_not_change_date(registry):
    ev = rensa_items(registry)[4]
    assert ev["date"] == "2026-11-27"
    assert ev["artist"] == "cinema staff 【8/29 振替公演】"
    assert ev["title"] == "前衛懐古主義 part 5"


def test_rensa_without_h2_uses_h1_as_title(registry):
    html = """<div class="monthly_schedule_primaly">
    <div class="schedule_date"><a name="schedule20261105">05</a></div>
    <div class="schedule_artist"><a href="../schedule_more/?index=1"><h1>単独の出演者</h1></a></div>
    </div><br />"""
    ev = venues.extract_items(BeautifulSoup(html, "html.parser"), registry["rensa"], RENSA_NOV, 2026, 11, TODAY)[0]
    assert ev["title"] == "単独の出演者" and ev["artist"] == "単独の出演者"


def test_rensa_secondary_must_directly_follow(registry):
    html = """<div class="monthly_schedule_primaly">
    <div class="schedule_date"><a name="schedule20261105">05</a></div>
    <div class="schedule_artist"><h1>前の行</h1></div></div>
    <br />
    <div class="monthly_schedule_secondary"><div class="schedule_note"><h2>別の公演</h2></div></div>"""
    ev = venues.extract_items(BeautifulSoup(html, "html.parser"), registry["rensa"], RENSA_NOV, 2026, 11, TODAY)[0]
    assert ev["title"] == "前の行"


def test_rensa_private_is_rejected_as_no_title(registry):
    private = rensa_items(registry)[3]
    assert private["date"] == "2026-11-19"
    assert private["title"] is None and private["blank_title"] is True and private["artist"] is None
    # 直後は <br> なので、次の公演の時刻を読まない
    assert private["start"] is None and private["link"] is None
    assert worker.judge_venue_event(private, registry["rensa"], IDX, TODAY)["reason"] == "公演名なし"


def test_rensa_does_not_change_the_parsed_page(registry):
    page = soup("rensa_202611.html")
    before = len(page.select(".monthly_schedule_secondary"))
    venues.extract_items(page, registry["rensa"], RENSA_NOV, 2026, 11, TODAY)
    assert len(page.select(".monthly_schedule_secondary")) == before


def test_rensa_month_pages_each_read_once(registry, monkeypatch):
    get = MagicMock(side_effect=lambda url, timeout=20: fixture("rensa_202611.html") if url == RENSA_NOV else None)
    monkeypatch.setattr(sites, "_get", get)
    events, warnings = venues.collect_venue(registry["rensa"], venues.Fetcher(), TODAY)
    assert get.call_count == 4    # 2026/11〜2027/02。詳細ページは読まない
    assert len(events) == 5
    assert sum("取得失敗" in w for w in warnings) == 3


# --- 抽出: EDWARD(同居型) ---

def edward_items(registry, vid):
    return venues.extract_items(soup("edward.html"), registry[vid], EDWARD_URL, today=TODAY)


def test_edward_one_fetch_serves_both_venues(registry, monkeypatch):
    get = MagicMock(return_value=fixture("edward.html"))
    monkeypatch.setattr(sites, "_get", get)
    fetcher = venues.Fetcher()
    darwin, _ = venues.collect_venue(registry["darwin"], fetcher, TODAY)
    daremo, _ = venues.collect_venue(registry["daremo"], fetcher, TODAY)
    assert get.call_count == 1
    assert len(darwin) == 3 and len(daremo) == 4


def test_edward_items_come_only_from_own_table_rows(registry):
    # 月スライダーの「11」「January」やニュースの「公演中止」を公演として拾わない
    titles = [i["title"] for i in edward_items(registry, "darwin")]
    assert titles == ["セントチヒロ・チッチ 僕らの逃避行。", "M-line Special 2026 Autumn 〜NOCTURNE〜",
                      "JAPAN TOUR from 171"]
    assert "ひとり昭和歌謡祭" in edward_items(registry, "daremo")[3]["title"]


def test_edward_date_from_th_fills_nearest_year(registry):
    assert [i["date"] for i in edward_items(registry, "darwin")] == ["2026-11-02", "2026-11-03", "2027-01-10"]
    assert [i["date"] for i in edward_items(registry, "daremo")] == [
        "2026-11-01", "2026-11-08", "2026-11-22", "2027-04-29"]


def test_edward_labelled_fields(registry):
    ev = edward_items(registry, "darwin")[0]
    assert ev["artist"] == "セントチヒロ・チッチ"
    assert (ev["open"], ev["start"]) == ("18:00", "18:30")


@pytest.mark.parametrize("vid,index,expected", [
    ("darwin", 1, ("14:30", "15:00")),     # ① 15:00 ② 17:45
    ("daremo", 1, ("12:45", "13:30")),     # ①13:30②17:00 (区切りなし)
    ("daremo", 3, ("11:30", "12:00")),     # ①12:00/②16:00
    ("daremo", 2, (None, "17:30")),        # OPEN 未定
    ("daremo", 0, ("17:00", "17:30")),     # 実ページの誤記 17;30
])
def test_edward_times(registry, vid, index, expected):
    ev = edward_items(registry, vid)[index]
    assert (ev["open"], ev["start"]) == expected


def test_edward_artist_keeps_values_after_label_only(registry):
    assert edward_items(registry, "darwin")[1]["artist"] == "譜久村聖・稲場愛香・小関舞・浅倉樹々 GUEST：森戸知沙希"
    assert edward_items(registry, "daremo")[3]["artist"] == "【出演】ティーナ・カリーナ"
    assert edward_items(registry, "darwin")[2]["artist"] == "171"


def test_failed_fetch_is_not_retried_for_sibling_venue(registry, monkeypatch):
    get = MagicMock(return_value=None)
    monkeypatch.setattr(sites, "_get", get)
    fetcher = venues.Fetcher()
    _, w1 = venues.collect_venue(registry["darwin"], fetcher, TODAY)
    _, w2 = venues.collect_venue(registry["daremo"], fetcher, TODAY)
    assert get.call_count == 1
    assert any("取得失敗" in w for w in w1) and any("取得失敗" in w for w in w2)


def test_missing_section_and_zero_items_warn(registry, monkeypatch, capsys):
    monkeypatch.setattr(sites, "_get", lambda url, timeout=20: "<html><body>改修後</body></html>")
    events, warnings = venues.collect_venue(registry["darwin"], venues.Fetcher(), TODAY)
    assert events == []
    assert any("抽出0件" in w for w in warnings)
    assert "⚠" in capsys.readouterr().out


def test_fetcher_stops_after_budget(monkeypatch):
    get = MagicMock(return_value="<html></html>")
    monkeypatch.setattr(sites, "_get", get)
    assert venues.Fetcher(budget_sec=-1).soup("https://example.com/") is None
    assert get.call_count == 0


# --- 判定の順序と hold の条件 ---

def judged(venue=None, **overrides):
    return worker.judge_venue_event(event(**overrides), venue or VENUE, IDX, TODAY)


@pytest.mark.parametrize("overrides,reason", [
    ({"title": ""}, "公演名なし"),
    ({"date": None}, "公演日なし"),
    ({"date": "2026-11-02"}, "公演日が過去"),
    ({"title": "SCHEDULE"}, "ページ見出し"),
    ({"title": "一般発売のご案内"}, "チケット受付"),
    ({"title": "FMラジオ公開収録"}, "放送・配信など"),
])
def test_basic_gate_rejects(overrides, reason):
    got = judged(**overrides)
    assert got["result"] == "reject" and got["reason"] == reason


def test_today_is_not_past():
    assert judged(date=TODAY, title="アイドルライブ")["result"] == "accept"


def test_cancelled_beats_catalog_match():
    assert judged(title="登録グループ 公演中止", artist="登録グループ")["result"] == "reject"
    assert judged(title="対バン", artist="登録グループ", item_text="公演中止")["result"] == "reject"


def test_rain_note_in_body_does_not_cancel():
    assert judged(title="アイドルライブ", body="雨天中止の場合は払い戻します")["result"] == "accept"


def test_venue_deny_beats_catalog_match():
    venue = dict(VENUE, filter={"deny_titles": ["コピーバンド"]})
    got = judged(venue, title="登録グループ コピーバンド大会", artist="登録グループ")
    assert got["result"] == "reject" and "コピーバンド" in got["reason"]


def test_catalog_match_beats_global_deny_word():
    got = judged(title="アイドル演劇公演", artist="登録グループ")
    assert got["result"] == "accept" and got["slugs"] == ["artist:登録グループ"]


def test_venue_allow_beats_global_deny_word():
    venue = dict(VENUE, filter={"allow_titles": ["演劇アイドル祭"]})
    got = judged(venue, title="演劇アイドル祭 vol.1")
    assert got["result"] == "accept" and got["slugs"] == []


def test_global_deny_beats_idol_word():
    assert judged(title="アイドル落語会")["result"] == "reject"


@pytest.mark.parametrize("title", ["地下アイドル祭", "IDOL NIGHT", "特典会あり", "ＣＨＥＫＩなし チェキ会"])
def test_idol_words_accept_as_unmatched(title):
    got = judged(title=title)
    assert got["result"] == "accept" and got["slugs"] == []


def test_idol_word_needs_word_boundary():
    assert judged(title="IDOLATRY TOUR")["result"] == "hold"


def test_no_evidence_is_hold():
    got = judged(title="Rock Night")
    assert got["result"] == "hold" and got["reason"] == "判定材料なし"


def test_short_english_name_in_title_only_is_hold():
    got = judged(title="BAND ワンマン")
    assert got["result"] == "hold" and "BAND" in got["reason"]


def test_short_english_name_in_body_only_is_hold():
    assert judged(title="Rock Night", body="ゲスト BAND")["result"] == "hold"


def test_short_english_name_in_lineup_is_accept():
    got = judged(title="Rock Night", artist="BAND / 未登録A")
    assert got["result"] == "accept" and got["slugs"] == ["artist:BAND"]


def test_short_english_hold_precedes_idol_word():
    assert judged(title="BAND アイドルナイト")["result"] == "hold"


def test_japanese_name_in_title_is_accept():
    got = judged(title="登録グループ ワンマン")
    assert got["result"] == "accept" and got["slugs"] == ["artist:登録グループ"]


# --- キー ---

def test_key_uses_stable_id_when_available():
    ev = event(ext_id="12345")
    assert worker.venue_base_key("ebeans", ev) == "12345"
    assert worker.venue_row_id("unmatched", "ebeans", "12345", "2026-11-10") == \
        "ven:unmatched:ebeans:12345:20261110"


def test_key_hash_ignores_title_notation():
    a = worker.venue_base_key("rensa", event(title="「仙台 アイドル祭」"))
    b = worker.venue_base_key("rensa", event(title="仙台アイドル祭"))
    assert a == b and len(a) == 12
    assert a != worker.venue_base_key("darwin", event(title="仙台アイドル祭"))


def test_same_name_same_day_gets_start_time_only_then():
    day = event(title="昼夜公演", start="12:00")
    night = event(title="昼夜公演", start="17:00")
    other = event(title="別の公演", start="18:00")
    worker.assign_venue_keys("rensa", [day, night, other])
    base = worker.venue_base_key("rensa", day)
    assert day["key"] == f"{base}-1200"
    assert night["key"] == f"{base}-1700"
    assert other["key"] == worker.venue_base_key("rensa", other)


def test_same_name_on_different_days_keeps_plain_key():
    a = event(title="定期公演", date="2026-11-10", start="18:00")
    b = event(title="定期公演", date="2026-11-11", start="19:00")
    worker.assign_venue_keys("rensa", [a, b])
    assert "-" not in a["key"] and "-" not in b["key"]


def test_key_problems_finds_duplicates_and_bad_format():
    good = "ven:artist:仙台flavor:rensa:a1b2c3d4e5f6:20261110"
    assert worker.venue_key_problems([good]) == []
    assert any("重複" in p for p in worker.venue_key_problems([good, good]))
    assert any("形式" in p for p in worker.venue_key_problems(["ven:unmatched:rensa:a b:2026"]))


def test_true_duplicates_are_flagged_in_report():
    evs = [event(title="アイドルライブ", start="18:00"), event(title="アイドルライブ", start="18:00")]
    report = worker.judge_venue_events(VENUE, evs, [], IDX, TODAY)
    assert any("重複" in w for w in report["warnings"])


# --- run_venues / main ---

@pytest.fixture
def run_env(monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(worker, "build_idol_name_list",
                        lambda: [{"name": "譜久村聖", "idol_slug": "artist:譜久村聖"},
                                 {"name": "枝豆順子", "idol_slug": "artist:枝豆順子"}])
    fake_sb = MagicMock()
    monkeypatch.setattr(worker, "sb", fake_sb)
    real_dt = worker.datetime

    class FixedDatetime(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 3, 18, 0, tzinfo=tz)

    fake_module = MagicMock(wraps=real_dt)
    fake_module.datetime = FixedDatetime
    fake_module.timedelta = real_dt.timedelta
    fake_module.timezone = real_dt.timezone
    fake_module.date = real_dt.date
    monkeypatch.setattr(worker, "datetime", fake_module)
    return summary, fake_sb


def test_run_venues_reports_without_writing_db(run_env, monkeypatch, capsys):
    summary, fake_sb = run_env
    monkeypatch.setenv("VENUE_IDS", "darwin,daremo")
    get = MagicMock(return_value=fixture("edward.html"))
    monkeypatch.setattr(sites, "_get", get)
    reports = worker.run_venues()
    assert fake_sb.mock_calls == []   # DB に一切触れない
    assert get.call_count == 1
    by_id = {r["id"]: r for r in reports}
    darwin, daremo = by_id["darwin"], by_id["daremo"]
    # 過去の公演はどちらも1件ずつ reject
    assert (darwin["fetched"], len(darwin["accept"]), len(darwin["hold"]), darwin["reject"]) == (3, 1, 1, 1)
    assert (daremo["fetched"], len(daremo["accept"]), len(daremo["hold"]), daremo["reject"]) == (4, 1, 2, 1)
    assert darwin["accept"][0]["keys"][0].startswith("ven:artist:譜久村聖:darwin:")
    assert darwin["accept"][0]["keys"][0].endswith(":20261103")
    assert darwin["hold"][0]["keys"][0].startswith("ven:unmatched:darwin:")
    text = summary.read_text(encoding="utf-8")
    assert "M-line Special" in text and "JAPAN TOUR from 171" in text and "図鑑に一致" in text
    assert "ven:artist:譜久村聖:darwin:" in text
    assert "僕らの逃避行" not in text   # reject は一覧に出さない
    assert "ven:artist:譜久村聖:darwin:" in capsys.readouterr().out


def test_run_venues_marks_failure_with_warning(run_env, monkeypatch, capsys):
    summary, _ = run_env
    monkeypatch.setenv("VENUE_IDS", "darwin")
    monkeypatch.setattr(sites, "_get", lambda url, timeout=20: None)
    reports = worker.run_venues()
    assert reports[0]["fetched"] == 0
    assert "⚠" in capsys.readouterr().out
    assert "⚠" in summary.read_text(encoding="utf-8")


def test_one_venue_error_does_not_stop_others(run_env, monkeypatch):
    monkeypatch.setenv("VENUE_IDS", "darwin,daremo")
    monkeypatch.setattr(sites, "_get", lambda url, timeout=20: fixture("edward.html"))
    real = venues.collect_venue

    def flaky(venue, fetcher, today):
        if venue["id"] == "darwin":
            raise RuntimeError("壊れた")
        return real(venue, fetcher, today)

    monkeypatch.setattr(venues, "collect_venue", flaky)
    by_id = {r["id"]: r for r in worker.run_venues()}
    assert any("壊れた" in w for w in by_id["darwin"]["warnings"])
    assert by_id["daremo"]["fetched"] == 4


# run_livepocket_sweep はフル巡回に加わった手順(LivePocket 開催日順の試運転)。ここで差し替えないと
# main() のテストが実際に一覧を取りに行く
FULL_STEPS = ("run_sites", "run_timetree", "run_gcal", "run_eplus", "run_curated", "run_yahoo_rt",
              "run_livepocket", "run_livepocket_sweep", "run_tiget", "run_release", "refresh_sale_info",
              "fix_ticket_urls", "cleanup_orphan_rows")


@pytest.fixture
def main_steps(monkeypatch):
    for name in ("X_ONLY", "DISCOVERY_ONLY", "QUICK_LISTING", "SITE_ONLY", "GCAL_ONLY", "LIVEPOCKET_ONLY",
                 "TIGET_ONLY", "RELEASE_ONLY", "SALE_ONLY", "LISTING_CHECK", "VENUE_ONLY",
                 "ENABLE_X_INGEST"):
        monkeypatch.delenv(name, raising=False)
    order = MagicMock()
    for name in FULL_STEPS + ("run_venues",):
        monkeypatch.setattr(worker, name, getattr(order, name))
    return order


def test_full_run_starts_with_venues(main_steps):
    assert worker.main() == 0
    called = [c[0] for c in main_steps.method_calls]
    assert called[:2] == ["run_venues", "run_sites"]
    assert called[-1] == "cleanup_orphan_rows"


def test_venue_failure_does_not_stop_full_run(main_steps, capsys):
    main_steps.run_venues.side_effect = RuntimeError("壊れた")
    assert worker.main() == 0
    assert [c[0] for c in main_steps.method_calls][1:] == list(FULL_STEPS)
    assert "⚠" in capsys.readouterr().out


def test_venue_only_runs_only_venues(main_steps, monkeypatch):
    monkeypatch.setenv("VENUE_ONLY", "true")
    assert worker.main() == 0
    assert [c[0] for c in main_steps.method_calls] == ["run_venues"]


@pytest.mark.parametrize("flag", ["DISCOVERY_ONLY", "QUICK_LISTING", "X_ONLY"])
def test_other_modes_do_not_run_venues(main_steps, monkeypatch, flag):
    monkeypatch.setenv(flag, "true")
    monkeypatch.setattr(worker, "share_sale_info", MagicMock())
    monkeypatch.setattr(worker, "sb", MagicMock())
    monkeypatch.setattr(worker, "run_x", MagicMock(), raising=False)
    monkeypatch.setattr(worker.asyncio, "run", MagicMock())
    worker.main()
    assert "run_venues" not in [c[0] for c in main_steps.method_calls]
