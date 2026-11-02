"""リリイベの購入整理券ページを取り込むための3点のテスト。

(1) 出演者の別表記 (2) ¥0 券種の保持 (3) 詳細ページの LivePocket リンクのガード。
ネットワークにも Supabase にも触れない。
"""
import os
import unicodedata
from unittest.mock import MagicMock

import pytest

# worker / sb は import 時に環境変数を読むので、実環境の設定を持ち込まない
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "YRT_FORCE"):
    os.environ.pop(_key, None)

import lineup  # noqa: E402
import ticketsite  # noqa: E402
import watchlist  # noqa: E402
import worker  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.abspath(watchlist.__file__))
TODAY = "2026-09-01"
SLUG = "artist:Mär mi"
LP_URL = "https://livepocket.jp/e/lakE01"
SITE = "https://marmi.tokyo/"
DETAIL = "https://marmi.tokyo/live_information/detail/46694"
SEIRIKEN_TITLE = "【Märmi】「Lost in Wonderland」リリース記念ツアーイベント@レイクタウン【対象商品購入の優先購入整理券】"


# --- (1) 出演者の別表記 ---

@pytest.fixture
def catalog(monkeypatch):
    """実物の name_aliases.json を読ませ、図鑑だけ差し替える。"""
    monkeypatch.chdir(REPO_ROOT)
    fake = MagicMock()
    fake.fetch_url_json.return_value = {"idols": [
        {"slug": SLUG, "name": "Mär mi"},
        {"slug": "artist:他のグループ", "name": "他のグループ"},
    ]}
    fake.get_approved_idol_info.return_value = []
    monkeypatch.setattr(watchlist, "sb", fake)
    return fake


@pytest.fixture
def fake_sb(monkeypatch):
    fake = MagicMock()
    fake.get_last_seen.return_value = None
    fake.get_auto_event_ids.return_value = []
    monkeypatch.setattr(worker, "sb", fake)
    return fake


def ingest(monkeypatch, fake_sb, performers, idols):
    info = {
        "event_name": SEIRIKEN_TITLE,
        "event_datetime": "2026-09-22 13:00",
        "venue": "イオンレイクタウン",
        "sale_datetime": "2026-09-01 12:00:00",
        "is_first_come": True,
        "ticket_types": [{"name": "購入整理券", "price": "0円"}],
        "performers": performers,
        "act_text": " / ".join(performers),
        "act_labeled": True,
    }
    monkeypatch.setattr(worker.ticketsite, "fetch_event", lambda _url: info)
    worker._ingest_sale_url("livepocket", LP_URL, idols, TODAY, set())
    return [r for call in fake_sb.upsert_auto_events.call_args_list for r in call.args[0]]


def test_only_listed_slug_gets_aliases(catalog):
    by_slug = {i["idol_slug"]: i for i in watchlist.build_idol_name_list()}
    assert by_slug[SLUG]["aliases"] == ["Mǎrmi"]
    assert "aliases" not in by_slug["artist:他のグループ"]


def test_aliases_attach_even_if_catalog_slug_is_decomposed(catalog):
    nfd = unicodedata.normalize("NFD", SLUG)
    catalog.fetch_url_json.return_value = {"idols": [{"slug": nfd, "name": "Mär mi"}]}
    assert watchlist.build_idol_name_list()[0]["aliases"] == ["Mǎrmi"]


def test_alias_slug_missing_from_catalog_is_reported(catalog, capsys):
    catalog.fetch_url_json.return_value = {"idols": [{"slug": "artist:他のグループ", "name": "他のグループ"}]}
    watchlist.build_idol_name_list()
    assert f"name_aliases.json の slug が図鑑にありません: ['{SLUG}']" in capsys.readouterr().out


def test_missing_alias_file_means_no_aliases(catalog, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert all("aliases" not in i for i in watchlist.build_idol_name_list())


@pytest.mark.parametrize("written", ["Mǎrmi", "Märmi", "Mär mi"])
def test_all_spellings_match_marmi(catalog, monkeypatch, fake_sb, written):
    rows = ingest(monkeypatch, fake_sb, [written], watchlist.build_idol_name_list())
    assert [r["tweet_id"] for r in rows] == ["yrt:artist:Mär mi:lp-lakE01:20260922"]
    assert rows[0]["idol_slug"] == SLUG


def test_caron_spelling_does_not_match_without_alias(monkeypatch, fake_sb):
    rows = ingest(monkeypatch, fake_sb, ["Mǎrmi"], [{"idol_slug": SLUG, "name": "Mär mi"}])
    assert [r["idol_slug"] for r in rows] == [worker.UNMATCHED_SLUG]


def test_lineup_counts_name_and_alias_as_one_group(catalog):
    idx = lineup.build_index(watchlist.build_idol_name_list())
    # 実ページの形: タイトルは「Märmi」、出演者欄は「Mǎrmi」
    assert lineup.names_in_text(f"{SEIRIKEN_TITLE}\nMǎrmi", idx) == [("Mär mi", SLUG)]
    assert lineup.names_in_text("Mǎrmi", idx) == [("Mär mi", SLUG)]


# --- (2) ¥0 券種の保持 ---

def lp_page(title, ticket_lines):
    """過去の購入整理券ページ(9/22 レイクタウン分)と同じ構造の LivePocket ページ。"""
    tickets = "".join(f"<div>{ln}</div>" for ln in ticket_lines)
    return f"""<html><head>
<script type="application/ld+json">{{"@type": "Event", "name": "{title}",
 "startDate": "2026-09-22T13:00:+0900",
 "location": {{"@type": "Place", "name": "イオンレイクタウン"}},
 "offers": {{"@type": "Offer", "price": "0"}}}}</script>
</head><body>
<a href="/event/search?performer=M%C7%8Ermi">Mǎrmi</a>
<h2>販売受付期間</h2>
<div>2026年9月1日(火) 12:00</div>
<div>〜2026年9月22日(火) 12:00</div>
<h2>チケット</h2>
<div>先着</div>
{tickets}
</body></html>"""


def fetch(monkeypatch, html):
    monkeypatch.setattr(ticketsite, "_get", lambda _url: html)
    return ticketsite.fetch_event(LP_URL)


def test_seiriken_free_ticket_and_sale_are_kept(monkeypatch):
    info = fetch(monkeypatch, lp_page(SEIRIKEN_TITLE, ["購入整理券", "販売中", "¥0"]))
    assert info["ticket_types"] == [{"name": "購入整理券", "price": "0円"}]
    assert info["sale_datetime"] == "2026-09-01 12:00:00"
    assert info["is_first_come"] is True
    assert info["performers"] == ["Mǎrmi"]


def test_ordinary_free_event_still_drops_free_ticket(monkeypatch):
    info = fetch(monkeypatch, lp_page("無料ライブ", ["入場チケット", "販売中", "¥0"]))
    assert info["ticket_types"] == []


def test_paid_tickets_are_unchanged(monkeypatch):
    info = fetch(monkeypatch, lp_page("通常公演", ["前方チケット", "販売中", "¥4,000"]))
    assert info["ticket_types"] == [{"name": "前方チケット", "price": "4,000円"}]


# --- (3) 詳細ページの LivePocket リンクのガード ---

def enrich(monkeypatch, body, **row_fields):
    monkeypatch.setattr(worker, "_detail_page_text", lambda _url: "2026年11月8日(日) 東京\n" + body)
    row = {"tweet_url": DETAIL, "event_datetime": "2026-11-08 13:00", "ticket_url": None,
           "open_time": None, "sale_datetime": None, "other_details": "KABUKICHO TOWER STAGE"}
    row.update(row_fields)
    return worker.enrich_site_row_from_detail(row, SITE)


def test_single_livepocket_link_is_taken(monkeypatch):
    row = enrich(monkeypatch, "https://livepocket.jp/e/xaa1q")
    assert row["ticket_url"] == "https://livepocket.jp/e/xaa1q"


def test_same_livepocket_page_written_twice_counts_as_one(monkeypatch):
    row = enrich(monkeypatch, "https://t.livepocket.jp/e/xaa1q\nhttps://livepocket.jp/e/xaa1q")
    assert row["ticket_url"] == "https://t.livepocket.jp/e/xaa1q"


def test_multiple_livepocket_links_are_not_taken(monkeypatch):
    row = enrich(monkeypatch, "https://livepocket.jp/e/xaa1q\nhttps://livepocket.jp/e/other")
    assert row["ticket_url"] is None


def test_other_site_is_still_taken_next_to_multiple_livepocket_links(monkeypatch):
    row = enrich(monkeypatch, "https://livepocket.jp/e/xaa1q\nhttps://livepocket.jp/e/other\n"
                              "https://tiget.net/events/123")
    assert row["ticket_url"] == "https://tiget.net/events/123"


def test_first_link_still_wins_when_only_one_livepocket(monkeypatch):
    row = enrich(monkeypatch, "https://tiget.net/events/123\nhttps://livepocket.jp/e/xaa1q")
    assert row["ticket_url"] == "https://tiget.net/events/123"


def test_stored_ticket_url_is_left_alone(monkeypatch):
    stored = "https://livepocket.jp/e/stored"
    row = enrich(monkeypatch, "https://livepocket.jp/e/xaa1q\nhttps://livepocket.jp/e/other",
                 ticket_url=stored)
    assert row["ticket_url"] == stored
