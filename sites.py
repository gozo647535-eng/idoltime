"""公式サイトからのイベント/チケット情報取得。

各アイドルの公式サイト(図鑑カタログの links に登録)を取得し、News/Schedule 等の
ページ本文を Gemini で解析して、今後のチケット/イベント情報を抽出する。
Xより壊れにくく、認証も不要。標準ライブラリ + bs4 + google-genai で動く。
"""
import gzip
import re
import time
import urllib.request
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# 抽出プロンプトを変更したらこの値を上げる(既読サイトの再抽出をトリガーする)
# v4: チケット無しの公演/ライブ出演予定も網羅対象に(イベント情報の取りこぼしを減らす)
# v5: JS描画対応 + Geminiのレート制限/再試行(既読サイトも本文が変わるので再抽出したい)
# v6: 抽出エンジンをGroq(高い無料枠)優先に切替+JSON出力形式を明記(全サイト再抽出)
# v7: Groqのnull許容(is_first_come等)+8b-instant/テキスト量削減(TPD対策)。全サイト再抽出。
# v8: 70bへ戻し+イベント関連行のみに凝縮してTPM(分あたりトークン)を大幅削減。全サイト再抽出。
# v9: Groqの旧モデル廃止(2026-08-16)で抽出が全滅していた期間の取りこぼしを回収する。
PROMPT_VERSION = "v9"

# 静的HTMLの可視テキストがこれ未満なら JS描画(SPA)とみなし、ヘッドレスで取り直す
RENDER_MIN_CHARS = 1500
# 1サイトあたりのヘッドレス描画ページ数の上限(home + サブページ)
RENDER_MAX_PAGES = 3

# Gemini 無料枠(gemini-2.5-flash は 20 req/分)を超えないよう、呼び出し間隔を空ける
_GEMINI_MIN_INTERVAL = 4.5  # 秒 → 最大 ~13 req/分(20/分に余裕を持たせる)
_gemini_last_call = [0.0]


def _gemini_throttle():
    dt = time.time() - _gemini_last_call[0]
    if dt < _GEMINI_MIN_INTERVAL:
        time.sleep(_GEMINI_MIN_INTERVAL - dt)
    _gemini_last_call[0] = time.time()


# 本日の無料枠を使い切ったと確定したら以降のGemini呼び出しを即スキップする(タイムアウト防止)。
# ハッシュ未更新のまま返すので、枠が回復した次回実行で取得される。
_quota_exhausted = [False]

# Groq(OpenAI互換)。無料枠が日次で桁違いに大きく(≒数千req/日)、Geminiの日次上限で
# 監視サイトの後半(いぎなり東北産等)に届かない「枠到達点問題」を回避するための一次エンジン。
# GROQ_API_KEY が設定されていれば worker 側で優先的に使われる。
import os as _os

# Groq は無料枠のモデルを定期的に廃止する(2026-08-16 に llama-3.1-8b-instant と
# llama-3.3-70b-versatile が廃止され、抽出が全滅した)。特定のモデル名に依存せず、
# 候補を順に試して通ったものを使い続ける。GROQ_MODEL でカンマ区切り指定すれば上書き可。
_GROQ_MODELS = [
    m.strip() for m in _os.environ.get(
        "GROQ_MODEL", "openai/gpt-oss-20b,openai/gpt-oss-120b,qwen/qwen3.6-27b"
    ).split(",") if m.strip()
]
# 実行中に確定した「使えるモデル」。None の間は _GROQ_MODELS を先頭から試す。
_groq_model_ok = [None]
# 存在しない/権限が無いと分かったモデル(同じ失敗を繰り返さない)
_groq_model_dead = set()


def _groq_candidates():
    if _groq_model_ok[0]:
        return [_groq_model_ok[0]]
    return [m for m in _GROQ_MODELS if m not in _groq_model_dead]


_GROQ_MIN_INTERVAL = 16.0  # 秒 → 最大 ~3.7 req/分(8bのTPM 6,000内に収める)
_groq_last_call = [0.0]
_groq_quota_exhausted = [False]


def _groq_throttle():
    dt = time.time() - _groq_last_call[0]
    if dt < _GROQ_MIN_INTERVAL:
        time.sleep(_GROQ_MIN_INTERVAL - dt)
    _groq_last_call[0] = time.time()

# News/Schedule/Ticket 系のサブページを見つけるためのキーワード
SUBPAGE_KEYWORDS = [
    "news", "schedule", "live", "ticket", "event", "information", "info",
    "tour", "oneman", "calendar", "concert", "gig",
    "スケジュール", "ニュース", "ライブ", "チケット", "イベント", "公演", "お知らせ",
    "ツアー", "ワンマン", "出演", "カレンダー", "日程",
]


def _get(url: str, timeout: int = 20) -> Optional[str]:
    """HTML/テキストを取得(gzip対応・文字コード推定)。失敗は None。"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept-Language": "ja,en;q=0.8"},
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        ctype = (r.headers.get("Content-Type") or "").lower()
        if not any(k in ctype for k in ("html", "xml", "text")):
            return None
        enc = "utf-8"
        m = re.search(r"charset=([\w\-]+)", ctype)
        if m:
            enc = m.group(1)
        return data.decode(enc, "replace")
    except Exception as e:
        print(f"    取得失敗 {url}: {e}")
        return None


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer"]):
        tag.decompose()
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _find_subpages(html: str, base_url: str) -> List[str]:
    """News/Schedule 等の同一ドメイン内サブページURLを最大3件返す。"""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    found: List[str] = []
    for a in soup.find_all("a", href=True):
        blob = (a["href"] + " " + (a.get_text() or "")).lower()
        if any(k in blob for k in SUBPAGE_KEYWORDS):
            full = urljoin(base_url, a["href"])
            if urlparse(full).netloc == base_host and full not in found and full != base_url:
                found.append(full)
        if len(found) >= 3:
            break
    return found


def _render_site(site_url: str) -> List[str]:
    """JS描画(SPA)サイトをヘッドレスChromiumで開き、home + 関連サブページの
    描画後の生HTMLを返す(可視テキストとJSON-LDの両方に使う)。
    playwright未導入・失敗時は [](静的処理にフォールバック)。
    ※GitHub Actions は直接ネット接続なのでプロキシ設定は不要。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    htmls: List[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page(user_agent=UA)

                def render(u: str) -> str:
                    page.goto(u, wait_until="domcontentloaded", timeout=25000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass  # networkidleに達しなくても現時点のDOMで十分
                    return page.content()

                home_html = render(site_url)
                htmls.append(home_html)
                subs = _find_subpages(home_html, site_url)
                for sub in subs[: RENDER_MAX_PAGES - 1]:
                    try:
                        htmls.append(render(sub))
                    except Exception as e:
                        print(f"    JSレンダリング失敗(サブ) {sub}: {e}")
            finally:
                browser.close()
    except Exception as e:
        print(f"    JSレンダリング失敗 {site_url}: {e}")
        return []
    return [h for h in htmls if h]


class TicketType(BaseModel):
    name: str = Field(description="券種名 (例: 一般, VIP)")
    price: str = Field(description="料金 (例: 5000円)")


class SiteEvent(BaseModel):
    event_name: str = Field(description="イベント名/公演名")
    event_datetime: Optional[str] = Field(None, description="公演日時/開演時刻 (例: 2026/08/20 18:30)")
    open_time: Optional[str] = Field(None, description="開場時刻。分かる場合のみ 'HH:MM' (例: 18:00)")
    entry_time: Optional[str] = Field(None, description="入場時刻。開場と別に指定がある場合のみ 'HH:MM'")
    sale_datetime: Optional[str] = Field(
        None,
        description="チケット発売/受付/先行の開始日時。分かる場合のみ 'YYYY-MM-DD HH:MM:SS'(年不明なら現在の年、秒は00)",
    )
    is_first_come: bool = Field(False, description="先着/一般発売(先着)なら True。抽選・先行(抽選)は False")
    ticket_url: Optional[str] = Field(None, description="チケット販売/詳細ページのURL")
    venue: Optional[str] = Field(None, description="会場")
    ticket_types: List[TicketType] = Field(default=[], description="券種と料金")

    # LLM(特にJSONモードのGroq)は必須でない項目に null を返すことがある。
    # null を既定値に丸めて、1項目の null で抽出全体が落ちるのを防ぐ。
    @field_validator("is_first_come", mode="before")
    @classmethod
    def _coerce_is_first_come(cls, v):
        return False if v is None else v

    @field_validator("ticket_types", mode="before")
    @classmethod
    def _coerce_ticket_types(cls, v):
        return [] if v is None else v


class SiteExtraction(BaseModel):
    events: List[SiteEvent] = Field(default=[], description="今後のチケット/イベント情報のリスト")


# ============================================================
# JSON-LD (schema.org Event) 抽出 — Gemini不要・高精度
# ============================================================
# 多くの公式サイト/チケットサイトは <script type="application/ld+json"> に
# schema.org の Event/MusicEvent 構造化データを埋め込んでいる。これを直接パースすれば
# Gemini を消費せずに正確な公演情報(日時・会場・チケット)を取得できる。

# schema.org で「イベント」とみなす @type(Festival 等 "Event" を含まない型も明示)
_JSONLD_EVENT_TYPES = {
    "Festival", "MusicFestival", "Concert", "Screening", "ExhibitionEvent",
}


def _jsonld_type_is_event(obj: dict) -> bool:
    t = obj.get("@type")
    types = t if isinstance(t, list) else [t]
    for x in types:
        if isinstance(x, str) and ("Event" in x or x in _JSONLD_EVENT_TYPES):
            return True
    return False


def _iter_jsonld_objects(data):
    """JSON-LD のトップ要素(dict/list/@graph)を再帰的に走査して dict を yield する。"""
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_objects(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_jsonld_objects(item)
        yield data


def _iso_display_datetime(s) -> Optional[str]:
    """ISO8601 → 'YYYY-MM-DD HH:MM'(時刻が無ければ 'YYYY-MM-DD')。"""
    s = str(s)
    dm = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if not dm:
        return None
    tm = re.search(r"T(\d{2}:\d{2})", s)
    return f"{dm.group(1)} {tm.group(1)}" if tm else dm.group(1)


def _iso_time(s) -> Optional[str]:
    tm = re.search(r"T(\d{2}:\d{2})", str(s))
    return tm.group(1) if tm else None


def _iso_sale_datetime(s) -> Optional[str]:
    """ISO8601 → 'YYYY-MM-DD HH:MM:SS'(build_site_row 側で timestamptz 化される)。"""
    s = str(s)
    dm = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if not dm:
        return None
    tm = re.search(r"T(\d{2}:\d{2}(?::\d{2})?)", s)
    if tm:
        t = tm.group(1)
        if len(t) == 5:
            t += ":00"
        return f"{dm.group(1)} {t}"
    return f"{dm.group(1)} 00:00:00"


def _ld_text(s) -> Optional[str]:
    """JSON-LD文字列の整形: HTMLエンティティ(&#038; 等)を復号し前後空白を詰める。"""
    import html as _html

    if s is None:
        return None
    t = _html.unescape(str(s)).strip()
    return t or None


def _jsonld_location_name(loc) -> Optional[str]:
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        return _ld_text(loc.get("name"))
    elif isinstance(loc, str):
        return _ld_text(loc)
    return None


def _jsonld_offers(offers):
    """offers(dict/list)から (ticket_url, sale_datetime, ticket_types) を取り出す。"""
    if not offers:
        return None, None, []
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return None, None, []
    url = None
    sale = None
    types: List[TicketType] = []
    for of in offers:
        if not isinstance(of, dict):
            continue
        if not url and of.get("url"):
            url = str(of["url"])
        if not sale:
            starts = of.get("availabilityStarts") or of.get("validFrom")
            if starts:
                sale = _iso_sale_datetime(starts)
        price = of.get("price")
        if price not in (None, "", 0):
            cur = of.get("priceCurrency") or "JPY"
            suffix = "円" if cur in ("JPY", "円") else f" {cur}"
            label = of.get("name") or of.get("category") or "チケット"
            types.append(TicketType(name=str(label), price=f"{price}{suffix}"))
    return url, sale, types


def _jsonld_to_site_event(obj: dict, today: str) -> Optional[SiteEvent]:
    name = obj.get("name")
    start = obj.get("startDate")
    if not name or not start:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(start))
    if not m:
        return None
    if m.group(1) < today:
        return None  # 終了済みの公演は除外
    door = obj.get("doorTime")
    ticket_url, sale_dt, ticket_types = _jsonld_offers(obj.get("offers"))
    return SiteEvent(
        event_name=_ld_text(name),
        event_datetime=_iso_display_datetime(start),
        open_time=_iso_time(door) if door else None,
        entry_time=None,
        sale_datetime=sale_dt,
        is_first_come=False,
        ticket_url=ticket_url,
        venue=_jsonld_location_name(obj.get("location")),
        ticket_types=ticket_types,
    )


def _parse_jsonld_events(html: str) -> List[SiteEvent]:
    """HTML中の schema.org Event 構造化データから今後の公演を抽出する(Gemini不要)。"""
    import datetime as _dt
    import json

    if not html:
        return []
    today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    soup = BeautifulSoup(html, "html.parser")
    out: List[SiteEvent] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue  # 壊れた/独自拡張のJSONは無視
        for obj in _iter_jsonld_objects(data):
            if not isinstance(obj, dict) or not _jsonld_type_is_event(obj):
                continue
            try:
                ev = _jsonld_to_site_event(obj, today)
            except Exception:
                ev = None
            if ev:
                out.append(ev)
    return out


# イベントに関係する行を見分けるためのシグナル(日付・時刻・チケット・公演語)
_EVENT_SIGNAL = re.compile(
    r"(20\d\d[./年]|\d{1,2}\s*[/月]\s*\d{1,2}|\d{1,2}\s*:\s*\d{2}|開演|開場|OPEN|START|"
    r"チケット|ticket|発売|受付|先行|一般販売|一般発売|抽選|先着|"
    r"LIVE|ライブ|ワンマン|公演|出演|会場|定期公演|ツアー|フェス|イベント)",
    re.IGNORECASE,
)


def _condense_for_llm(text: str, limit: int = 2000) -> str:
    """LLMへ渡す前に、イベント関連(日付・時刻・チケット・公演)の行だけを前後1行の文脈付きで
    抽出し、トークン量を大幅に削減する(Groq無料枠のTPM=分あたりトークン上限対策)。
    シグナルが乏しいページは元テキスト先頭を返す(取りこぼし防止)。"""
    lines = text.split("\n")
    keep = set()
    for i, ln in enumerate(lines):
        if _EVENT_SIGNAL.search(ln):
            for j in range(max(0, i - 1), min(len(lines), i + 2)):
                keep.add(j)
    if len(keep) < 3:
        return text[:limit]
    picked = "\n".join(lines[i] for i in sorted(keep))
    return picked[:limit]


def _build_extract_prompt(name: str, text: str, today: str) -> str:
    """公式サイト本文からの抽出プロンプト(Gemini/Groq 共通)。"""
    return f"""以下は「{name}」の公式サイトから抜き出したイベント関連テキストです。本日は {today}(JST)です。
このグループが**今後出演するライブ・イベント(公演)**を、正確なJSONで網羅的に抽出してください。チケット情報の有無に関わらず、開催日が特定できる出演予定はすべて拾ってください。

出力は次の形の JSON オブジェクトのみ(前後に説明文やコードフェンスを付けない):
{{"events": [{{"event_name": str, "event_datetime": str, "open_time": str|null, "entry_time": str|null, "sale_datetime": str|null, "is_first_come": bool, "ticket_url": str|null, "venue": str|null, "ticket_types": [{{"name": str, "price": str}}]}}]}}

厳守事項:
- 開催日が {today} より前の、すでに終了した公演は**絶対に含めない**(過去の公演は除外)
- event_datetime(公演日時)は必須。'YYYY-MM-DD HH:MM' か、時刻不明なら 'YYYY-MM-DD'。年が不明なら {today[:4]} を補完(月日が過去なら翌年)
- **実際のライブ/イベント出演のみ**。グッズ・写真集・CD/配信リリース・MV/動画公開・TV/ラジオ/雑誌などの露出情報は含めない
- チケットが分かる場合のみ: sale_datetime(発売/受付/先行/抽選申込の開始日時)は 'YYYY-MM-DD HH:MM:SS' 形式、is_first_come、ticket_types、ticket_url を埋める。無ければ null/空でよい
- open_time(開場)・entry_time(入場)は本文に明記がある場合のみ 'HH:MM'(無ければ null)
- venue(会場)は分かれば入れる。同じ公演を重複させない
- 該当が無ければ events は空配列にする

【イベント関連テキスト】
{_condense_for_llm(text)}"""


def _today_jst() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d")


def extract_events_groq(groq_client, name: str, text: str) -> Optional[List[SiteEvent]]:
    """Groq(OpenAI互換・高い無料枠)で今後の公演を抽出する。
    返り値の意味は extract_events と同じ([]=該当なし / None=一時失敗で次回再試行)。"""
    import json

    if not groq_client or not text.strip():
        return []
    if _groq_quota_exhausted[0]:
        return None
    prompt = _build_extract_prompt(name, text, _today_jst())
    for attempt in range(3):
        try:
            _groq_throttle()
            cands = _groq_candidates()
            if not cands:
                print("    Groq: 使えるモデルがありません(全候補が利用不可)")
                return None
            res = None
            last_404 = None
            for model in cands:
                try:
                    res = groq_client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": "あなたはイベント情報抽出器です。指示されたJSONオブジェクトのみを返します。"},
                            {"role": "user", "content": prompt},
                        ],
                        response_format={"type": "json_object"},
                        temperature=0.1,
                    )
                except Exception as me:
                    msg_m = str(me)
                    if "model_not_found" in msg_m or "does not exist" in msg_m:
                        # 廃止済みモデル。以後は試さず次の候補へ。
                        _groq_model_dead.add(model)
                        last_404 = msg_m
                        print(f"    Groq: モデル {model} は利用不可。次の候補を試します")
                        continue
                    raise
                if _groq_model_ok[0] != model:
                    _groq_model_ok[0] = model
                    print(f"    Groq: モデル {model} を使用します")
                break
            if res is None:
                print(f"    Groq: 全モデル候補が利用不可: {last_404}")
                return None
            raw = res.choices[0].message.content or "{}"
            data = json.loads(raw)
            return SiteExtraction(**data).events
        except Exception as e:
            msg = str(e)
            is_quota = "429" in msg or "rate_limit" in msg.lower() or "rate limit" in msg.lower()
            if is_quota and attempt < 2:
                m = re.search(r"try again in ([\d.]+)s", msg)
                wait = min(float(m.group(1)) + 1, 65) if m else 20
                print(f"    Groq 429(レート上限)。{wait:.0f}s 待って再試行 ({attempt + 1}/2)")
                time.sleep(wait)
                continue
            if is_quota:
                _groq_quota_exhausted[0] = True
                print("    Groq枠が回復せず。以降のサイトはスキップ(次回実行で取得)")
                return None
            print(f"    Groq抽出失敗: {e}")
            return None if any(k in msg.lower() for k in ("deadline", "timeout", "timed out")) else []
    return None


def extract_events_any(groq_client, gemini_client, name: str, text: str) -> Optional[List[SiteEvent]]:
    """抽出の統合エントリ。高枠の Groq を優先し、未設定/枠切れ/一時失敗なら Gemini にフォールバック。"""
    if groq_client:
        r = extract_events_groq(groq_client, name, text)
        if r is not None:
            return r
        # Groq が枠切れ/一時失敗 → Gemini があれば補完的に試す(無ければ None のまま次回再試行)
        if gemini_client:
            return extract_events(gemini_client, name, text)
        return None
    return extract_events(gemini_client, name, text)


def extract_events(gemini_client, name: str, text: str) -> Optional[List[SiteEvent]]:
    """公式サイト本文から今後の公演を抽出する。
    返り値: 抽出結果のリスト([] は「該当なし」)。Geminiの一時失敗(429/通信エラー)で
    確定できなかった場合は None(呼び出し側は状態を更新せず次回再試行する)。"""
    if not gemini_client or not text.strip():
        return []
    if _quota_exhausted[0]:
        return None  # 本日の無料枠切れ確定 → 呼ばずにスキップ(次回実行で取得)
    from google.genai import types

    today = _today_jst()
    prompt = _build_extract_prompt(name, text, today)
    # 429 は主に「分あたり20件」の一時的な上限。指示された待機時間(最大65s)をきちんと待って
    # 再試行すれば通る。3回とも待っても通らなければ恒常的な枠切れとみなし、以降はスキップする。
    for attempt in range(3):
        try:
            _gemini_throttle()
            res = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SiteExtraction,
                    temperature=0.1,
                ),
            )
            return res.parsed.events
        except Exception as e:
            msg = str(e)
            is_quota = "429" in msg or "RESOURCE_EXHAUSTED" in msg
            if is_quota and attempt < 2:
                m = re.search(r"retry in ([\d.]+)s", msg)
                wait = min(float(m.group(1)) + 1, 65) if m else 30
                print(f"    Gemini 429(分あたり上限)。{wait:.0f}s 待って再試行 ({attempt + 1}/2)")
                time.sleep(wait)
                continue
            if is_quota:
                # 3回とも(毎回フル待機して)429 → 恒常的な枠切れとみなし以降スキップ(タイムアウト防止)
                _quota_exhausted[0] = True
                print("    Gemini枠が回復せず。以降のサイトはスキップ(次回実行で取得)")
                return None
            print(f"    Gemini抽出失敗: {e}")
            return None if "deadline" in msg.lower() else []
    return None


def fetch_site_content(site_url: str):
    """公式サイト(+ News/Schedule サブページ)を取得し (content_hash, combined_text, jsonld_events)
    を返す。取得できなければ (None, '', [])。**Geminiは呼ばない**(呼び出し側で content_hash を見て
    変化があるときだけ extract_events を呼ぶことで、無料枠の消費を最小化する)。
    静的HTMLの可視テキストが極端に短い(JS描画のSPA)場合は、ヘッドレスChromiumで
    描画し直す(いぎなり東北産・RE-GE のような予定表がJSで描画されるサイト対策)。
    併せて生HTML中の schema.org Event(JSON-LD)を Gemini不要で抽出して返す。"""
    import hashlib

    home = _get(site_url)
    home_text = _html_to_text(home) if home else ""
    htmls: List[str] = []

    if len(home_text) < RENDER_MIN_CHARS:
        # SPAの可能性 → ヘッドレスで取り直す。描画で本文が増えればそれ(生HTML)を使う。
        rendered = _render_site(site_url)
        rendered_text_len = sum(len(_html_to_text(h)) for h in rendered)
        if rendered_text_len > len(home_text):
            print(f"    JS描画で取得(静的{len(home_text)}字→描画{rendered_text_len}字)")
            htmls = rendered
        else:
            if home:
                htmls.append(home)
            for sub in (_find_subpages(home, site_url) if home else []):
                h = _get(sub)
                if h:
                    htmls.append(h)
    else:
        htmls = [home]
        for sub in _find_subpages(home, site_url):
            h = _get(sub)
            if h:
                htmls.append(h)

    texts = [t for t in (_html_to_text(h) for h in htmls) if t]

    # JSON-LD(schema.org Event)を抽出。名前+日時で重複除去。
    jsonld_events: List[SiteEvent] = []
    seen_ld = set()
    for h in htmls:
        for ev in _parse_jsonld_events(h):
            k = (ev.event_name, ev.event_datetime)
            if k not in seen_ld:
                seen_ld.add(k)
                jsonld_events.append(ev)
    if jsonld_events:
        print(f"    JSON-LDから {len(jsonld_events)} 件のイベントを検出")

    if not texts and not jsonld_events:
        return None, "", []
    combined = "\n\n".join(texts)
    # PROMPT_VERSION と JSON-LD署名を含めることで、本文/構造化データが変わると再抽出が走る。
    # JS描画で本文が変われば content_hash も変わるため、既読SPAサイトも自動で再抽出される。
    ld_sig = "|".join(f"{e.event_name}@{e.event_datetime}" for e in jsonld_events)
    content_hash = hashlib.sha1(
        (PROMPT_VERSION + "\n" + combined + "\n" + ld_sig).encode("utf-8", "ignore")
    ).hexdigest()
    return content_hash, combined, jsonld_events
