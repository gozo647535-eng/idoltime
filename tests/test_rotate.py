"""巡回ローテーション(_rotate)の配分テスト。設計は案B(2026-11-08):
直前組(IMMINENT_DAYS 日内に公演)は②の床(REST_FLOOR_SHARE)を引いた残り
全部まで広げ、②には必ず床の分を残す。

背景: 以前は直前6割固定で、直前組が27組を超えると公演前日の組でも
落とされていた(ギミラブ！が 2026-11-06 の巡回で未処理になった構造)。
ネットワークにも Supabase にも触れない。sb の関数はすべて差し替える。
"""
import os

import pytest

for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "YRT_FORCE"):
    os.environ.pop(_key, None)

import worker  # noqa: E402

TODAY = "2026-11-08"
BATCH = 45  # TT_BATCH の既定値


def _watch(prefix, n):
    return [{"idol_slug": f"{prefix}{i:03d}"} for i in range(n)]


def _run(monkeypatch, watch, next_dates, counts=None, last_seen=None, batch=BATCH):
    counts = counts or {}
    last_seen = last_seen or {}
    monkeypatch.setattr(worker.sb, "get_upcoming_counts", lambda today: counts)
    monkeypatch.setattr(worker.sb, "get_last_seen_by_prefix", lambda prefix: last_seen)
    monkeypatch.setattr(worker.sb, "get_next_event_dates", lambda today: next_dates)
    monkeypatch.setattr(worker.sb, "set_last_seen_many", lambda pairs: None)
    return worker._rotate(watch, TODAY, "tq", batch, "TimeTree", record=False)


def _slugs(entries):
    return [e["idol_slug"] for e in entries]


def test_soon_overflow_displaces_rest_beyond_old_cap(monkeypatch):
    """直前40組・残り30組: 直前は36組まで入り、残りは床の9組だけになる。"""
    soon = _watch("s", 40)
    rest = _watch("r", 30)
    # 直前組は全員 11/09〜11/11(4日以内)に公演。日付順に s000 が一番近い
    next_dates = {e["idol_slug"]: f"2026-11-{9 + i % 3:02d}" for i, e in enumerate(soon)}
    next_dates.update({e["idol_slug"]: "2026-12-20" for e in rest})
    picked = _run(monkeypatch, soon + rest, next_dates)
    picked_slugs = _slugs(picked)
    assert len(picked) == BATCH
    assert sum(1 for s in picked_slugs if s.startswith("s")) == 36
    assert sum(1 for s in picked_slugs if s.startswith("r")) == 9


def test_next_day_group_ranked_30th_in_soon_is_picked(monkeypatch):
    """回帰: 旧配分(直前27組)では落ちた「直前30番目・翌日公演」が入る。"""
    soon = _watch("s", 30)
    rest = _watch("r", 30)
    # 全員が翌日公演。対象だけ last_seen が一番新しく、直前リストの最後尾(30番目)に並ぶ
    next_dates = {e["idol_slug"]: "2026-11-09" for e in soon}
    target = soon[-1]["idol_slug"]
    last_seen = {f"tq:{e['idol_slug']}": "2026-11-01" for e in soon}
    last_seen[f"tq:{target}"] = "2026-11-07"
    next_dates.update({e["idol_slug"]: "2026-12-20" for e in rest})
    picked = _run(monkeypatch, soon + rest, next_dates, last_seen=last_seen)
    assert target in _slugs(picked)


def test_rest_fills_beyond_floor_when_soon_is_small(monkeypatch):
    """直前が少ない回は、残りが床を超えて枠を埋める。"""
    soon = _watch("s", 10)
    rest = _watch("r", 60)
    next_dates = {e["idol_slug"]: "2026-11-09" for e in soon}
    next_dates.update({e["idol_slug"]: "2026-12-20" for e in rest})
    picked = _run(monkeypatch, soon + rest, next_dates)
    picked_slugs = _slugs(picked)
    assert len(picked) == BATCH
    assert sum(1 for s in picked_slugs if s.startswith("s")) == 10
    assert sum(1 for s in picked_slugs if s.startswith("r")) == 35


def test_no_soon_picks_rest_only(monkeypatch):
    soon = []
    rest = _watch("r", 100)
    next_dates = {e["idol_slug"]: "2026-12-20" for e in rest}
    picked = _run(monkeypatch, rest, next_dates)
    assert len(picked) == BATCH
    assert all(s.startswith("r") for s in _slugs(picked))


def test_no_rest_picks_soon_up_to_batch(monkeypatch):
    soon = _watch("s", 50)
    next_dates = {e["idol_slug"]: "2026-11-09" for e in soon}
    picked = _run(monkeypatch, soon, next_dates)
    assert len(picked) == BATCH
    assert all(s.startswith("s") for s in _slugs(picked))


def test_new_calendar_comes_first_in_rest(monkeypatch):
    """新規登録(予定0件・巡回記録なし)は残り枠の先頭に来て必ず拾われる。"""
    soon = _watch("s", 40)
    rest = _watch("r", 30)
    newbie = {"idol_slug": "newbie"}
    watch = soon + rest + [newbie]
    next_dates = {e["idol_slug"]: "2026-11-09" for e in soon}
    next_dates.update({e["idol_slug"]: "2026-12-20" for e in rest})
    counts = {e["idol_slug"]: 5 for e in rest}
    last_seen = {f"tq:{e['idol_slug']}": "2026-11-05" for e in rest}
    picked = _run(monkeypatch, watch, next_dates, counts=counts, last_seen=last_seen)
    assert "newbie" in _slugs(picked)


def test_dropped_soon_count_is_logged(monkeypatch, capsys):
    soon = _watch("s", 40)
    rest = _watch("r", 30)
    next_dates = {e["idol_slug"]: "2026-11-09" for e in soon}
    next_dates.update({e["idol_slug"]: "2026-12-20" for e in rest})
    _run(monkeypatch, soon + rest, next_dates)
    out = capsys.readouterr().out
    assert "次回送り 4 件" in out
