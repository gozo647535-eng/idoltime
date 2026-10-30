"""公式サイトからのイベント/チケット情報取得。

各アイドルの公式サイト(図鑑カタログの links に登録)を取得し、News/Schedule 等の
ページ本文を Gemini で解析して、今後のチケット/イベント情報を抽出する。
Xより壊れにくく、認証も不要。標準ライブラリ + bs4 + google-genai で動く。
"""
import gzip
import re
import time
import urllib.parse
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
# v10: JSONモード非対応モデルへのフォールバックを追加し、失敗していたサイトを再抽出する。
# v11: 0件のまま固定化していたサイト(RE-GE等)を取り直す。
# v12: スケジュール用サブページもJS描画するようにし、カレンダーページを読めるようにする。
# v13: 凝縮の優先順位を見直し、今後の日付を含む箇所を先に残す。
# v14: チケット受付を独立イベントにせず、公演のsale_datetimeへ入れる指示を追加。
# v15: JSON破損で0件確定していたサイトを取り直す(120bモデル+再生成の修正を反映)。
PROMPT_VERSION = "v28"  # 公演ページを予定の項目の日付からも探すので再抽出

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
        "GROQ_MODEL", "openai/gpt-oss-120b,openai/gpt-oss-20b"
    ).split(",") if m.strip()
]
# 実行中に確定した「使えるモデル」。None の間は _GROQ_MODELS を先頭から試す。
_groq_model_ok = [None]
# 存在しない/権限が無いと分かったモデル(同じ失敗を繰り返さない)
_groq_model_dead = set()
# 枠(TPD/TPM)を使い切ったモデル。モデルごとに別枠なので、1つ落ちても次の候補で続行する。
_groq_model_quota = set()


# JSONモードを受け付けないモデルがある(gpt-oss-20b は response_format 指定だと
# "Failed to validate JSON" で400になる)。その場合は素の生成に切り替えるため記録する。
_groq_no_json_mode = set()

# 出力TPMが小さいモデル向けに、モデルごとの max_tokens 上限を覚える。
_groq_max_tokens = {}


# 出力がこれを下回るモデルは、1サイト分のイベントを書き切れない。足切りに使う。
_GROQ_MIN_USABLE_CAP = 2000


def _groq_cap_for(model: str) -> int:
    default = int(_os.environ.get("GROQ_MAX_TOKENS", "6000"))
    return min(_groq_max_tokens.get(model, default), default)


def _groq_note_too_large(model: str, msg: str) -> bool:
    """「要求が大きすぎる」系の429を見て、そのモデルの上限を下げる。

    qwen のように出力TPMが小さいモデルでは max_tokens 自体が上限を超え、
    何秒待っても必ず429になる。待つのではなく上限を下げて投げ直す。
    返り値: 上限を下げられた(=やり直す価値がある)か。
    """
    low = msg.lower()
    if "too large" not in low and "max_tokens" not in low:
        return False
    cur = _groq_cap_for(model)
    m = (re.search(r"equal to\D{0,3}(\d+)", msg)
         or re.search(r"[Ll]imit\s+(\d+)", msg))
    if m:
        cap = int(m.group(1))
        if cap >= cur:
            return False  # 既に上限以下。別の原因なので下げ続けない
    else:
        cap = cur // 2
    cap = max(600, cap)
    if cap >= cur:
        return False
    _groq_max_tokens[model] = cap
    print(f"    Groq: {model} の出力上限に合わせて max_tokens を {cur} → {cap} に下げます")
    return True


def _pydantic_error():
    """pydantic の検証エラー型(無ければ決して一致しないダミー)。"""
    try:
        from pydantic import ValidationError
        return ValidationError
    except Exception:
        class _None(Exception):
            pass
        return _None


def _escape_inner_quotes(t: str) -> str:
    """文字列値の中に素のまま現れた二重引用符をエスケープする。

    JSONモードを受け付けないモデルは、イベント名に含まれる " をそのまま書いてしまい
    JSON 全体が壊れる。閉じ引用符なら直後が , : } ] のいずれかになるはずなので、
    そうでない " は値の一部とみなして \" に直す。
    """
    out = []
    in_str = False
    i = 0
    while i < len(t):
        ch = t[i]
        if ch == "\\" and in_str and i + 1 < len(t):
            out.append(t[i:i + 2])
            i += 2
            continue
        if ch == '"':
            if not in_str:
                in_str = True
            else:
                nxt = next((c for c in t[i + 1:] if not c.isspace()), "")
                if nxt in ",:}]" or nxt == "":
                    in_str = False
                else:
                    out.append("\\")  # 値の途中の " → エスケープして残す
        out.append(ch)
        i += 1
    return "".join(out)


def _salvage_truncated(t: str):
    """出力が max_tokens で途中で切れた JSON を、最後に完結した要素までで救う。"""
    import json as _json
    ends = [m.end() for m in re.finditer(r"\}", t)][-40:]
    for end in reversed(ends):
        head = t[:end]
        for closing in ("]}", "}]}"):
            for cand in (head + closing, _escape_inner_quotes(head) + closing):
                try:
                    data = _json.loads(cand)
                except ValueError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("events"), list):
                    print(f"    Groq: 出力が途中で切れていたため {len(data['events'])} 件まで救出しました")
                    return data
    return None


def _lenient_json(raw: str):
    """コードフェンスや前後の説明が混じっていてもJSONオブジェクトを取り出す。
    素の引用符で壊れている場合は直してからもう一度読む。"""
    import json as _json
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    for cand in (t, _escape_inner_quotes(t)):
        try:
            return _json.loads(cand)
        except ValueError:
            pass
    salvaged = _salvage_truncated(t)
    if salvaged is not None:
        return salvaged
    return _json.loads(_escape_inner_quotes(t))  # 元の例外を投げ直す


# API から取得した実在モデル一覧(候補を推測で書くと存在しないモデルで詰む)
_groq_discovered = [None]

# 一覧には音声合成(canopylabs/orpheus-*)やアラビア語専用(allam-*)も混ざる。
# 名前の除外では取りこぼすので、日本語の抽出に使える汎用チャットLLMの系統だけを許可する。
_GROQ_ALLOW_FAMILIES = ("gpt-oss", "llama", "qwen", "kimi", "deepseek",
                        "mixtral", "mistral", "gemma")
# 同じ系統名でも、分類器・検閲モデル・音声系は抽出に使えない
# (llama-prompt-guard・gpt-oss-safeguard は context_window が 512)。
_GROQ_DENY_KINDS = ("guard", "moderation", "whisper", "tts", "orpheus",
                    "embed", "rerank", "vision", "ocr", "asr")
# 抽出プロンプトを収めるのに必要な最低限の文脈長
_GROQ_MIN_WINDOW = 16000
_GROQ_PREFER = ("gpt-oss-120b", "gpt-oss-20b", "llama-4", "llama-3.3", "llama",
                "qwen", "kimi", "deepseek", "mixtral", "mistral", "gemma")


def _groq_model_window(m) -> int:
    """モデルの文脈長を読む(分類器は 512 程度なので、これで実用品と区別できる)。"""
    src = {}
    try:
        src = m.model_dump()
    except Exception:
        src = dict(getattr(m, "__dict__", {}) or {})
    for k in ("context_window", "max_context_window_tokens", "context_length"):
        v = getattr(m, k, None)
        if not isinstance(v, int):
            v = src.get(k)
        if isinstance(v, int) and v > 0:
            return v
    return 0


def _groq_discover(client) -> List[str]:
    """Groq で実際に使えるチャットモデルを一覧取得する(廃止・改名に追従するため)。"""
    if _groq_discovered[0] is not None:
        return _groq_discovered[0]
    ids: List[str] = []
    try:
        for m in client.models.list().data:
            mid = getattr(m, "id", "") or ""
            low = mid.lower()
            if not mid or not any(k in low for k in _GROQ_ALLOW_FAMILIES):
                continue
            if any(k in low for k in _GROQ_DENY_KINDS):
                continue
            win = _groq_model_window(m)
            if win and win < _GROQ_MIN_WINDOW:
                continue
            ids.append(mid)
    except Exception as e:
        print(f"    Groq: モデル一覧を取得できませんでした: {str(e)[:120]}")

    def _rank(mid: str) -> int:
        low = mid.lower()
        for i, k in enumerate(_GROQ_PREFER):
            if k in low:
                return i
        return len(_GROQ_PREFER)

    ids.sort(key=_rank)
    _groq_discovered[0] = ids
    if ids:
        print(f"    Groq: 利用可能なモデル {len(ids)} 件 → {', '.join(ids[:6])}")
    return ids


def _groq_candidates(client=None):
    if _groq_model_ok[0] and _groq_model_ok[0] not in _groq_model_quota:
        return [_groq_model_ok[0]]
    pool = list(_GROQ_MODELS)
    if client is not None:
        for mid in _groq_discover(client):
            if mid not in pool:
                pool.append(mid)
    return [m for m in pool
            if m not in _groq_model_dead and m not in _groq_model_quota]


def _groq_retry_wait(msg: str) -> float:
    """429本文の "try again in 1m30s" / "...in 12.5s" から待ち時間(秒)を読む。"""
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", msg)
    if not m:
        return 65.0
    mins = float(m.group(1) or 0)
    return mins * 60 + float(m.group(2))


def _groq_is_daily_quota(msg: str) -> bool:
    """日次枠(TPD/RPD)切れか。待っても明けないのでモデルを切り替える。"""
    low = msg.lower()
    return ("per day" in low or "tpd" in low or "rpd" in low
            or _groq_retry_wait(msg) > 300)


_GROQ_MIN_INTERVAL = 16.0  # 秒 → 最大 ~3.7 req/分(8bのTPM 6,000内に収める)
_groq_last_call = [0.0]
_groq_quota_exhausted = [False]


def _groq_throttle():
    dt = time.time() - _groq_last_call[0]
    if dt < _GROQ_MIN_INTERVAL:
        time.sleep(_GROQ_MIN_INTERVAL - dt)
    _groq_last_call[0] = time.time()

# News/Schedule/Ticket 系のサブページを見つけるためのキーワード
# サブページの選び方は関連度順にする。文書順に拾うとナビの並び次第で
# スケジュールページが漏れ、ニュースやグッズで枠が埋まってしまう。
# 上の段ほど「予定そのもの」が載っている可能性が高い。
SUBPAGE_TIERS = [
    ["schedule", "calendar", "スケジュール", "カレンダー", "日程",
     "tour", "ツアー", "live", "ライブ", "公演", "oneman", "ワンマン", "concert", "gig",
     # 単独公演の特設ページ。ここだけに公演日が書かれていることがある
     # (いぎなり東北産のアリーナライブは /pages/iginari-arena にしか日付が無い)
     "arena", "アリーナ", "特設", "hall", "ホール", "anniversary", "周年", "生誕"],
    ["event", "イベント", "出演"],
    ["ticket", "チケット", "news", "ニュース", "information", "info", "お知らせ"],
]
# /contents/1113873 のような記事の個別URL。トップの新着欄で入れ替わるため、
# 同じ階層の固定ページより後ろに回す(入れ替わるたびに特設ページが押し出されていた)。
_NEWS_PERMALINK = re.compile(r"/\d{5,}/?$")
MAX_SUBPAGES = 5
SUBPAGE_KEYWORDS = [k for tier in SUBPAGE_TIERS for k in tier]


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


PAGE_MARK = "【ページ】"

# 凝縮後の素材に日付がいくつ残ったかを数えるためのパターン(ログ用)
_DATE_IN_TEXT = re.compile(r"20\d\d[./\-]\d{1,2}[./\-]\d{1,2}")


def _page_title(html: str) -> str:
    """ページの見出し(h1 → og:title → title)。公演名として使えることがある。"""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    h1 = soup.find("h1")
    if h1 and (h1.get_text() or "").strip():
        return re.sub(r"\s+", " ", h1.get_text()).strip()[:120]
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and (og.get("content") or "").strip():
        return re.sub(r"\s+", " ", og["content"]).strip()[:120]
    if soup.title and (soup.title.get_text() or "").strip():
        return re.sub(r"\s+", " ", soup.title.get_text()).strip()[:120]
    return ""


# ページではないリンク(カレンダーの単発ICS・資料・画像など)
_NOT_A_PAGE = re.compile(
    r"\.(ics|pdf|jpe?g|png|gif|webp|svg|zip|mp4|mp3|docx?|xlsx?)$", re.IGNORECASE)


def _find_subpages(html: str, base_url: str) -> List[str]:
    """同一ドメイン内のサブページURLを、予定が載っていそうな順に返す。

    順位は (tier, 記事の個別URLか) で決める。tier だけで並べると、同じ tier の中では
    出現順になり、トップの新着欄で入れ替わる記事URLが固定ページを押し出してしまう。
    """
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    ranked = {}
    for a in soup.find_all("a", href=True):
        blob = (a["href"] + " " + (a.get_text() or "")).lower()
        tier = next((i for i, ks in enumerate(SUBPAGE_TIERS) if any(k in blob for k in ks)), None)
        if tier is None:
            continue
        full = urljoin(base_url, a["href"])
        # 同じページの別アンカー(#ticket 等)は同一ページなので断片を落として揃える
        full = full.split("#", 1)[0]
        # 末尾のスラッシュ有無で同じページを2回取らないよう揃える
        if full.endswith("/") and urlparse(full).path != "/":
            full = full.rstrip("/")
        if urlparse(full).netloc != base_host or full.rstrip("/") == base_url.rstrip("/"):
            continue
        if _NOT_A_PAGE.search(urlparse(full).path):
            continue   # ICS・PDF・画像などはページではない
        if full not in ranked or tier < ranked[full]:
            ranked[full] = tier
    order = {u: i for i, u in enumerate(ranked)}
    return sorted(
        ranked, key=lambda u: (ranked[u], bool(_NEWS_PERMALINK.search(urlparse(u).path)), order[u])
    )[:MAX_SUBPAGES]


def _render_page(url: str) -> Optional[str]:
    """1ページだけヘッドレスChromiumで描画して生HTMLを返す。失敗時 None。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page(user_agent=UA)
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        print(f"    JSレンダリング失敗(サブ) {url}: {e}")
        return None


def _render_site(site_url: str) -> List[tuple]:
    """JS描画(SPA)サイトをヘッドレスChromiumで開き、home + 関連サブページの
    (URL, 描画後の生HTML) を返す(可視テキストとJSON-LDの両方に使う)。
    playwright未導入・失敗時は [](静的処理にフォールバック)。
    ※GitHub Actions は直接ネット接続なのでプロキシ設定は不要。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    htmls: List[tuple] = []   # (URL, 描画後HTML)
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
                htmls.append((site_url, home_html))
                subs = _find_subpages(home_html, site_url)
                for sub in subs[: RENDER_MAX_PAGES - 1]:
                    try:
                        htmls.append((sub, render(sub)))
                    except Exception as e:
                        print(f"    JSレンダリング失敗(サブ) {sub}: {e}")
            finally:
                browser.close()
    except Exception as e:
        print(f"    JSレンダリング失敗 {site_url}: {e}")
        return []
    return [(u, h) for u, h in htmls if h]


class TicketType(BaseModel):
    name: str = Field(description="券種名 (例: 一般, VIP)")
    price: str = Field("", description="料金 (例: 5000円)")

    # 料金未定の券種に null を返すモデルがある。1項目の null で抽出全体を
    # 落とすのは割に合わないので、空文字に丸める。
    @field_validator("name", "price", mode="before")
    @classmethod
    def _coerce_str(cls, v):
        return "" if v is None else v


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
    # その公演の記述があったページのURL。アプリでイベントから飛ぶ先に使う
    # (チケットURLが無い公演は、サイトのトップではなくこのページへ案内したい)。
    source_url: Optional[str] = Field(None, description="その公演の記述があったページのURL")
    source_page: Optional[str] = Field(None, description="その公演の記述があった【ページ】の番号(P1 など)")

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


def _ld_url(v) -> Optional[str]:
    """JSON-LD の url。http(s) の絶対URLのときだけ採る。"""
    u = v if isinstance(v, str) else (v.get("url") if isinstance(v, dict) else None)
    u = (u or "").strip()
    return u if u.startswith("http") else None


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
        # 構造化データが公演ページのURLを持っていれば、それを案内先にする
        source_url=_ld_url(obj.get("url")),
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
# 「10.05」と「MON.2026」のように、月日と年が別の要素に分かれて書かれている予定表。
# <time class="md">10.05</time><time class="year">MON.2026</time> のような作りは多く、
# このままでは日付として認識できず(ドット区切りの月日はどの正規表現にも当たらない)、
# 予定のかたまりが「日付なし」と見なされて凝縮で捨てられていた(≒JOY 等)。
_SPLIT_MD = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})\s*(?:[（(][^）)]{1,4}[）)])?\s*$")
_SPLIT_YEAR = re.compile(r"^\s*[^\d]{0,8}(20\d\d)\s*年?\s*$")


def stitch_split_dates(text: str) -> str:
    """月日だけの行と年だけの行が隣り合っている箇所を『2026.10.05』に組み直す。

    どちら向き(月日→年 / 年→月日)にも並ぶので両方見る。組み直した行は
    既存の『20\d\d[./年]…』の判定に当たるようになる。
    """
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        md = _SPLIT_MD.match(ln)
        if not md:
            continue
        for j in (i + 1, i - 1):
            if not 0 <= j < len(lines):
                continue
            ym = _SPLIT_YEAR.match(lines[j])
            if not ym:
                continue
            mm, dd = int(md.group(1)), int(md.group(2))
            if not (1 <= mm <= 12 and 1 <= dd <= 31):
                break
            lines[i] = f"{ym.group(1)}.{mm:02d}.{dd:02d}{ln.strip()[len(md.group(1)) + len(md.group(2)) + 1:]}"
            break
    return "\n".join(lines)


# 月めくりカレンダー型の予定表。見出しに「10 / October / 2026」があり、以降は
# 日のマスが数字だけの行として並ぶ(≒JOY の /schedule/ など)。数字だけでは日付と
# 判定できないため、見出しから年月を引き継いで『2026.10.12』に組み直す。
_MONTH_WORDS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_LINE = re.compile(r"^\s*([A-Za-z]{3,9})\.?\s*$|^\s*(\d{1,2})\s*月\s*$")
# 「2026年10月」のように年と月が1行に入った見出し(HKT48 の予定表など)
_YEAR_MONTH_LINE = re.compile(r"^\s*(20\d\d)\s*[年./\-]\s*(\d{1,2})\s*月?\s*$")
_YEAR_LINE = re.compile(r"^\s*(20\d\d)\s*年?\s*$")
_DAY_LINE = re.compile(r"^\s*(\d{1,2})\s*$")
_MIN_CALENDAR_RUN = 5


def _month_of(line: str):
    m = _MONTH_LINE.match(line)
    if not m:
        return None
    if m.group(2):
        mm = int(m.group(2))
        return mm if 1 <= mm <= 12 else None
    return _MONTH_WORDS.get(m.group(1).lower())


def stitch_calendar_days(text: str) -> str:
    """カレンダー型の予定表の、数字だけの日のマスを『YYYY.MM.DD』に書き換える。

    月見出しの前後には前月・翌月への切り替え(「9」「11」など)も数字だけで並ぶので、
    数字行をそのまま日付にはできない。月見出し以降の数字行のうち、
    **単調増加が最も長く続くまとまり**だけを日のマスと見なす。
    """
    lines = text.split("\n")
    year = None
    sections = []  # (month, year, [数字行のindex])
    for i, ln in enumerate(lines):
        both = _YEAR_MONTH_LINE.match(ln)
        if both and 1 <= int(both.group(2)) <= 12:
            year = int(both.group(1))
            sections.append((int(both.group(2)), year, []))
            continue
        ym = _YEAR_LINE.match(ln)
        if ym:
            year = int(ym.group(1))
            continue
        mm = _month_of(ln)
        if mm:
            sections.append((mm, year, []))
            continue
        if sections and _DAY_LINE.match(ln):
            sections[-1][2].append(i)
    import datetime as _dt
    for month, yr, idxs in sections:
        if not yr or len(idxs) < _MIN_CALENDAR_RUN:
            continue
        best, run = [], []
        for i in idxs:
            d = int(lines[i].strip())
            if run and d <= int(lines[run[-1]].strip()):
                if len(run) > len(best):
                    best = run
                run = []
            if 1 <= d <= 31:
                run.append(i)
        if len(run) > len(best):
            best = run
        if len(best) < _MIN_CALENDAR_RUN:
            continue
        for i in best:
            d = int(lines[i].strip())
            try:
                _dt.date(yr, month, d)
            except ValueError:
                continue
            lines[i] = f"{yr}.{month:02d}.{d:02d}"
    return "\n".join(lines)


def drop_empty_date_lines(text: str) -> str:
    """中身の無い日付行を落とす。

    日付ごとの一覧になっている予定表では、予定の無い日も日付だけの行として並ぶ。
    これを残すと LLM が前後の公演をその日に結びつけてしまう。実例として
    超ときめき♡宣伝部の予定表から、本当は10/05だけの武道館公演が10/05〜10/09の
    5日間として、10/24だけの有明アリーナ公演が14日間として出力された。

    次の日付行までの間に中身(曜日だけの行を除く)が無い日付行を落とす。
    """
    lines = text.split("\n")
    def is_date(ln):
        return bool(_PURE_DATE_LINE.match(ln.replace("\t", " ")))
    def is_filler(ln):
        t = ln.strip()
        return not t or _WEEKDAY_LINE.match(t)
    out = []
    for i, ln in enumerate(lines):
        if is_date(ln):
            has_body = False
            for nxt in lines[i + 1:]:
                if is_date(nxt):
                    break
                if not is_filler(nxt):
                    has_body = True
                    break
            if not has_body:
                continue
        out.append(ln)
    return "\n".join(out)


_EVENT_SIGNAL = re.compile(
    r"(20\d\d[./年]|\d{1,2}\s*[/月]\s*\d{1,2}|\d{1,2}\s*:\s*\d{2}|開演|開場|OPEN|START|"
    r"チケット|ticket|発売|受付|先行|一般販売|一般発売|抽選|先着|"
    r"LIVE|ライブ|ワンマン|公演|出演|会場|定期公演|ツアー|フェス|イベント)",
    re.IGNORECASE,
)


# 予定と無関係で、しかもイベント語を含むために凝縮をすり抜けてしまう行
_CONDENSE_NOISE = re.compile(
    r"(WALLPAPER|壁紙|お絵かき|伝言板|セットリスト|SETLIST|メンバー紹介|プロフィール|"
    r"ディスコグラ|DISCOGRAPHY|MAGAZINE|GOODS|グッズ|通販|抽選結果|会員|ファンクラブ|"
    # TV・ラジオ・雑誌等の露出はプロンプトでも抽出対象外。字数を食うだけなので先に落とす
    r"^TV$|テレビ|ラジオ|RADIO|放送|番組|雑誌|配信中|公開中|"
    # 新譜・主題歌・動画の告知。日付が付いて並ぶので予定表と競合して字数を食う
    r"RELEASE|リリース決定|主題歌|配信リリース|MV|YOUTUBE|VIEW MORE|"
    # 劇場の休館・休演は予定ではないが、カレンダーでは毎日並ぶので字数を食う
    r"休館|休演|定休)",
    re.IGNORECASE)


# 受付・発売の告知行。プロンプト上これ自体はイベントにならないので、字数の割り当ては
# 実公演より後回しにする(公演日が別の場所に書かれているサイトで、受付行だけが残って
# 肝心の開催日が押し出される事故を防ぐ)。
_REAL_DATE_LINE = re.compile(
    r"(\d{4}\s*[./年]\s*\d{1,2}\s*[./月]\s*\d{1,2}|open\s*\d|start\s*\d|開場|開演)",
    re.IGNORECASE)

# カレンダーの日付のマス(日付だけの行)と、曜日だけの行
_PURE_DATE_LINE = re.compile(r"^\s*20\d\d[./\-]\d{1,2}[./\-]\d{1,2}\s*$")
_WEEKDAY_LINE = re.compile(r"^\s*[（(]?[月火水木金土日]([・/][月火水木金土日])*[）)]?\s*$")

# 予定表のページを見分ける見出し(【ページ】行に対して使う)
_SCHEDULE_PAGE_RE = re.compile(
    r"(schedule|スケジュール|calendar|カレンダー|予定|日程|公演|ライブ|live|出演|event|tour|ツアー)",
    re.IGNORECASE)

_RECEPTION_LINE = re.compile(
    r"(先行受付|先行発売|一般発売|プレイガイド|抽選申込|二次先行|チケット発売|受付中|受付開始|発売中)")


def _condense_for_llm(text: str, limit: int = 2200) -> str:
    """LLM へ渡す前に、イベント関連の箇所だけを抜き出してトークン量を削る
    (無料枠の分/日あたり上限への対策)。

    限られた字数に何を残すかが肝心で、素直に先頭から詰めると、新着のお知らせや
    壁紙・セットリストで埋まり、肝心のスケジュールが押し出される。そこで
    **今後の日付を含むかたまりを先に**入れ、余った分を他のイベント行で埋める。
    日付と公演名は別の行に分かれていることが多いので、前後1行をひとかたまりとして
    扱い、組にしたまま残す(日付だけが並んで名前が消えるのを防ぐ)。
    """
    # 描画したページは行頭にタブが残る(「\t\t\t2026.11.08」)。そのままだと日付だけの
    # 行と分からず、日ごとのかたまりにならない(超ときめき♡宣伝部の11月が丸ごと落ちた)
    lines = [ln.strip() for ln in text.split("\n")]
    page_marks = [i for i, ln in enumerate(lines) if ln.startswith(PAGE_MARK)]
    blocks = []
    for i, ln in enumerate(lines):
        # カレンダー形式の予定表は、日付のマスの下にその日の予定が数行続く。
        # 前後1行だけを見ると日付と曜日しか残らず、肝心の公演名(3〜4行下)が
        # 落ちる(HKT48 は31日分すべてが「劇場 休館日」だけになっていた)。
        # 次の日付行までを、その日のひとかたまりとして扱う。
        if _PURE_DATE_LINE.match(ln):
            end, j = i + 1, i + 1
            while j < len(lines) and j <= i + 7 and not _PURE_DATE_LINE.match(lines[j]):
                end, j = j + 1, j + 1
            day = [k for k in range(i, end)
                   if not _CONDENSE_NOISE.search(lines[k]) and not _WEEKDAY_LINE.match(lines[k])]
            if len(day) > 1:   # 日付だけで中身が無い日は入れない
                blocks.append(day)
            continue
        if not _EVENT_SIGNAL.search(ln) or ln.startswith(PAGE_MARK):
            continue
        block = range(max(0, i - 1), min(len(lines), i + 2))
        # 種になる行だけでなく、かたまり全体で判定する。日付行の隣に "TV" と
        # 書いてあるだけの放送予定などは、文脈行として紛れ込んでくるため。
        if any(_CONDENSE_NOISE.search(lines[j]) for j in block):
            continue
        blocks.append(block)
    if len(blocks) < 2:
        return text[:limit]

    # どのページのかたまりかを覚えておく。予定表のページを先に入れたい
    # (トップページの新着やチケット受付に字数を食われ、肝心の /schedule の
    #  中身が1行も入らないことがあった)。
    page_of = {}
    cur_page = -1
    for i, ln in enumerate(lines):
        if ln.startswith(PAGE_MARK):
            cur_page = i
        page_of[i] = cur_page

    def _on_schedule_page(b) -> bool:
        h = page_of.get(min(b), -1)
        return h >= 0 and bool(_SCHEDULE_PAGE_RE.search(lines[h]))

    picked, used = set(page_marks), 0  # ページ見出しは必ず残す(数行で安い)

    def take(block):
        nonlocal used
        new = [i for i in block if i not in picked]
        size = sum(len(lines[i]) + 1 for i in new)
        if not new or used + size > limit:
            return
        picked.update(new)
        used += size

    def _is_reception(b) -> bool:
        return (any(_RECEPTION_LINE.search(lines[i]) for i in b)
                and not any(_REAL_DATE_LINE.search(lines[i]) for i in b))

    future = [b for b in blocks if any(has_future_dates(lines[i]) for i in b)]
    # ① 予定表ページの、今後の日付を含む公演のかたまりを最優先で入れる
    for b in future:
        if not _is_reception(b) and _on_schedule_page(b):
            take(b)
    # ② 次に他のページ(トップの新着など)の公演のかたまり
    for b in future:
        if not _is_reception(b):
            take(b)
    # ③ 次に受付告知(sale_datetime の材料になる)
    for b in future:
        take(b)
    # ④ 余力で残りのイベント行
    for b in blocks:
        take(b)

    # 残した行には、その行がぶら下がっている日付行を必ず添える。
    # かたまりは独立に選ばれるため、公演名のかたまりだけが残って日付行が落ちると、
    # 前に出てくる別の日付にぶら下がって見える(「GFEST.2026」が10/10から10/09に
    # ずれた)。日付行は短いので、足しても字数はほとんど増えない。
    owner = {}
    cur_date = None
    for i, ln in enumerate(lines):
        if ln.startswith(PAGE_MARK):
            cur_date = None
        elif _PURE_DATE_LINE.match(ln):
            cur_date = i
        owner[i] = cur_date
    for i in sorted(picked):
        d = owner.get(i)
        if d is not None:
            picked.add(d)

    # 中身が1行も残らなかった日付行は落とす(残すと LLM が前後の公演を
    # その日に結びつけてしまう)
    return drop_empty_date_lines("\n".join(lines[i] for i in sorted(picked)))


def _build_extract_prompt(name: str, text: str, today: str) -> str:
    """公式サイト本文からの抽出プロンプト(Gemini/Groq 共通)。"""
    return f"""以下は「{name}」の公式サイトから抜き出したイベント関連テキストです。本日は {today}(JST)です。
このグループが**今後出演するライブ・イベント(公演)**を、正確なJSONで網羅的に抽出してください。チケット情報の有無に関わらず、開催日が特定できる出演予定はすべて拾ってください。

出力は次の形の JSON オブジェクトのみ(前後に説明文やコードフェンスを付けない)。
文字列の中では二重引用符(")を使わず、必要なら『』や「」に置き換えること(JSONが壊れるため):
{{"events": [{{"event_name": str, "event_datetime": str, "open_time": str|null, "entry_time": str|null, "sale_datetime": str|null, "is_first_come": bool, "ticket_url": str|null, "venue": str|null, "ticket_types": [{{"name": str, "price": str}}]}}]}}

厳守事項:
- 開催日が {today} より前の、すでに終了した公演は**絶対に含めない**(過去の公演は除外)
- event_datetime(公演日時)は必須。'YYYY-MM-DD HH:MM' か、時刻不明なら 'YYYY-MM-DD'。年が不明なら {today[:4]} を補完(月日が過去なら翌年)
- **実際のライブ/イベント出演のみ**。グッズ・写真集・CD/配信リリース・MV/動画公開・TV/ラジオ/雑誌などの露出情報は含めない
- **チケットの受付・発売それ自体はイベントではない**。「先行受付」「一般発売」「プレイガイド受付」「抽選申込」「二次先行」等は、
  その対象となる**公演の sale_datetime(受付開始日時)として記録**し、独立した events の要素には**絶対にしない**。
  受付期間の日付を event_datetime に入れてはいけない(例: 「9/24〜10/4 Aライブ先行受付」は 9/24 開催の公演ではない)
- 同じ公演に複数の受付(先行・一般等)がある場合も**公演は1件**にまとめ、最も早い受付開始を sale_datetime にする
- 受付情報しか無く**公演日そのものが本文から特定できない公演は、出力しない**(受付期間で代用しない)
- チケットが分かる場合のみ: sale_datetime(発売/受付/先行/抽選申込の開始日時)は 'YYYY-MM-DD HH:MM:SS' 形式、is_first_come、ticket_types、ticket_url を埋める。無ければ null/空でよい
- open_time(開場)・entry_time(入場)は本文に明記がある場合のみ 'HH:MM'(無ければ null)
- venue(会場)は分かれば入れる。同じ公演を重複させない
- 行頭が「【ページ】」の行は、以降の記述がどのページから来たかを示す見出しである。
  日付や open/start だけが並んでいて公演名が見当たらない場合は、直前の「【ページ】」の
  見出しを公演名として使ってよい(特設ページは公演名が見出しにしか無いことがある)
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
    tried_model = [None]
    for attempt in range(3):
        try:
            _groq_throttle()
            cands = _groq_candidates(groq_client)
            # 出力上限が小さすぎるモデルは、正しいJSONのまま一部のイベントを
            # 黙って落とす(途中切れの痕跡も残らない)。その結果でハッシュを確定
            # させると、より充実していた既存の行を削除してしまうので使わない。
            usable = [m for m in cands if _groq_cap_for(m) >= _GROQ_MIN_USABLE_CAP]
            if cands and not usable:
                print(f"    Groq: 残る候補の出力上限が小さすぎます({', '.join(f'{m}:{_groq_cap_for(m)}' for m in cands)})。"
                      "欠けた結果を確定させないため次回に回します")
                return None
            cands = usable
            if not cands:
                print("    Groq: 使えるモデルがありません(全候補が利用不可)")
                return None
            res = None
            last_err = None
            tried_model[0] = cands[0]
            for model in cands:
                tried_model[0] = model
                # JSONモード → 失敗したら素の生成、の順で試す
                modes = [False] if model in _groq_no_json_mode else [True, False]
                for strict in modes:
                    kwargs = dict(
                        model=model,
                        messages=[
                            {"role": "system", "content": "あなたはイベント情報抽出器です。指示されたJSONオブジェクトのみを返します。"},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.1,
                        max_tokens=_groq_cap_for(model),
                    )
                    if strict:
                        kwargs["response_format"] = {"type": "json_object"}
                    try:
                        res = groq_client.chat.completions.create(**kwargs)
                        break
                    except Exception as me:
                        msg_m = str(me)
                        last_err = msg_m
                        if ("model_terms_required" in msg_m or "terms acceptance" in msg_m
                                    or "model_not_found" in msg_m or "does not exist" in msg_m):
                            _groq_model_dead.add(model)
                            print(f"    Groq: モデル {model} は利用不可。次の候補を試します")
                            res = None
                            break  # このモデルは諦めて次の候補へ
                        if strict and ("validate JSON" in msg_m or "json_validate_failed" in msg_m):
                            # このモデルはJSONモード非対応。以後は素の生成で呼ぶ。
                            _groq_no_json_mode.add(model)
                            print(f"    Groq: モデル {model} はJSONモード非対応。素の生成で再試行します")
                            res = None
                            continue
                        raise
                if res is not None:
                    if _groq_model_ok[0] != model:
                        _groq_model_ok[0] = model
                        print(f"    Groq: モデル {model} を使用します")
                    break
            if res is None:
                print(f"    Groq: 全モデル候補が利用不可: {last_err}")
                return None
            raw = res.choices[0].message.content or "{}"
            data = _lenient_json(raw)
            return SiteExtraction(**data).events
        except Exception as e:
            msg = str(e)
            is_quota = "429" in msg or "rate_limit" in msg.lower() or "rate limit" in msg.lower()
            # 「max_tokens が大きすぎる」は待っても直らない。上限に合わせて投げ直す。
            if (not is_quota and tried_model[0] and "max_tokens" in msg
                    and _groq_note_too_large(tried_model[0], msg) and attempt < 2):
                continue
            if is_quota:
                # 枠切れではなく「1回の要求が上限より大きい」場合は待っても無駄。
                if tried_model[0] and _groq_note_too_large(tried_model[0], msg):
                    continue
                daily = _groq_is_daily_quota(msg)
                if not daily and attempt < 2:
                    # 分単位の窓は待てば明ける。指示された時間をきちんと待つ。
                    wait = min(_groq_retry_wait(msg) + 1, 65)
                    print(f"    Groq 429(レート上限)。{wait:.0f}s 待って再試行 ({attempt + 1}/2): {msg[:180]}")
                    time.sleep(wait)
                    continue
                # 日次枠切れ(または待っても明けない) → このモデルは打ち止め。
                # モデルごとに別枠なので、残っている候補に切り替えて続行する。
                if tried_model[0]:
                    _groq_model_quota.add(tried_model[0])
                    if _groq_model_ok[0] == tried_model[0]:
                        _groq_model_ok[0] = None
                nxt = _groq_candidates(groq_client)
                if nxt:
                    print(f"    Groq: {tried_model[0]} の枠切れ({'日次' if daily else '回復せず'})。{nxt[0]} に切り替えます")
                    if attempt < 2:
                        continue
                    return None
                _groq_quota_exhausted[0] = True
                print("    Groq枠が回復せず(全モデル)。以降のサイトはスキップ(次回実行で取得)")
                return None
            # 壊れたJSON(多くは本文の引用符がエスケープされていない)は毎回出るとは限らない。
            # 生成し直せば通ることが多いので、同じ要求をもう一度投げる。
            is_bad_json = isinstance(e, ValueError) and not isinstance(e, _pydantic_error())
            if is_bad_json and attempt < 2:
                print(f"    Groq: JSONが壊れていたため再生成します ({attempt + 1}/2): {msg[:120]}")
                continue
            print(f"    Groq抽出失敗: {e}")
            # 失敗は「イベントが無い」ではない。[] を返すとハッシュが確定し、
            # それまでに取れていた行まで削除されてしまう(v16 初回実行で実際に起きた)。
            # 0件と確定できるのは、応答を正しく解析できて events が空だったときだけ。
            return None
    return None


# 本文に出てくる日付。年つき(2026.10.05 / 2026年10月5日)と年なし(10/5 / 10月5日)の両方
_TEXT_DATE_FULL = re.compile(r"20(\d\d)\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})")
# 区切りは広めに採る。ここで拾いすぎても検算が緩くなるだけで、正しい公演は落とさない
_TEXT_DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[./\-ー月]\s*(\d{1,2})(?!\d)")


def dates_in_text(text: str):
    """本文に実在する (月, 日) の集合。抽出結果の検算に使う。"""
    out = set()
    for m in _TEXT_DATE_FULL.finditer(text or ""):
        out.add((int(m.group(2)), int(m.group(3))))
    for m in _TEXT_DATE_MD.finditer(text or ""):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.add((mo, d))
    return out


# 同じ公演名がこの日数以上に増えたら、日付を本文と突き合わせて検算する
_EXPANDED_EVENT_DAYS = 3


def drop_invented_dates(events: List["SiteEvent"], text: str, name: str = ""):
    """同じ公演が何日にも増やされたとき、本文に無い日付を落とす。

    LLM は公演名と日付の対応を作ってしまうことがある。実例として
    超ときめき♡宣伝部の公式サイトから、本当は1日(10/05)の武道館公演が
    10/05〜10/09 の5日間として、10/24 の有明アリーナ公演が14日間として
    出力された。

    ただし日付の書き方は多様で(「10/4・5」のような連記もある)、本文に無い
    という理由だけで落とすと正しい公演まで消しかねない。増殖の形になっている
    公演名(_EXPANDED_EVENT_DAYS 日以上)に限って検算する。
    ツアーや劇場公演で本当に複数日あるものは、日付が本文に書かれているので残る。
    """
    if not events:
        return events
    present = dates_in_text(text)
    if not present:
        return events  # 日付を1つも読めない本文では検算しない
    import collections
    days = collections.Counter(_norm_event_title(e.event_name) for e in events)
    kept, dropped = [], []
    for ev in events:
        md = _event_month_day(ev)
        expanded = days[_norm_event_title(ev.event_name)] >= _EXPANDED_EVENT_DAYS
        if md is None or not expanded or md in present:
            kept.append(ev)
        else:
            dropped.append(ev)
    if dropped:
        shown = " / ".join(f"{(e.event_datetime or '')[:10]}" for e in dropped[:6])
        print(f"    本文に無い日付の公演を {len(dropped)} 件落としました: {shown}"
              + (" ほか" if len(dropped) > 6 else ""))
    return kept


def _norm_event_title(name) -> str:
    return re.sub(r"[\s　「」『』【】()（）!！?？・,、.。]+", "", (name or "")).lower()


def _event_month_day(ev: "SiteEvent"):
    m = re.match(r"\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", ev.event_datetime or "")
    if not m:
        return None
    return (int(m.group(2)), int(m.group(3)))


def extract_events_any(groq_client, gemini_client, name: str, text: str) -> Optional[List[SiteEvent]]:
    """抽出の統合エントリ。高枠の Groq を優先し、未設定/枠切れ/一時失敗なら Gemini にフォールバック。

    どちらの結果も、本文に無い日付は落としてから返す(LLM が日付を作ってしまう)。
    """
    if groq_client:
        r = extract_events_groq(groq_client, name, text)
        if r is not None:
            return drop_invented_dates(r, text, name)
        # Groq が枠切れ/一時失敗 → Gemini があれば補完的に試す(無ければ None のまま次回再試行)
        if gemini_client:
            r = extract_events(gemini_client, name, text)
            return None if r is None else drop_invented_dates(r, text, name)
        return None
    r = extract_events(gemini_client, name, text)
    return None if r is None else drop_invented_dates(r, text, name)


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
            return None  # 失敗を0件として確定させない(既存の行が消える)
    return None


def _fetch_subpage(url: str):
    """サブページを取得する。静的の可視テキストが薄ければ JS 描画で取り直す。

    トップが充実していてもスケジュールだけカレンダー用の別ページにあり、そこが
    JS 描画という作りが多い(いぎなり東北産の /vertical_calendar など)。
    静的取得だけでは公演日がまったく拾えないので、そこだけ描画する。
    """
    h = _get(url)
    if h is not None and len(_html_to_text(h)) >= RENDER_MIN_CHARS:
        return h
    rendered = _render_page(url)
    if rendered and len(_html_to_text(rendered)) > len(_html_to_text(h) if h else ""):
        print(f"    JS描画でサブページ取得: {url}")
        return rendered
    return h


# 月ごとの予定表で、翌月・翌々月を読むページの数
NEXT_MONTH_PAGES = 2


def _next_month_pages(pages, base_url: str) -> List[str]:
    """月ごとに分かれた予定表の、翌月・翌々月のページのURL。

    予定表が「今月」しか出さない作りのサイトでは、月末になると今後の公演が
    翌月のページにしか無い(超ときめき♡宣伝部の /calendar は今月だけで、
    11月の公演は /vertical_calendar/2026/11 にあった)。読んだページのリンクから、
    URL の終わり(またはクエリ)が翌月・翌々月の年月になっているものを探す。
    「/2026-11-1」のような日ごとの記事は年月で終わらないので拾わない。
    """
    import datetime as _dt
    today = _dt.date.fromisoformat(_today_jst())
    months = []
    y, m = today.year, today.month
    for _ in range(NEXT_MONTH_PAGES):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        months.append((y, m))
    host = urlparse(base_url).netloc
    have = {u.rstrip("/") for u, _h in pages}
    out = []
    for y, m in months:
        pat = re.compile(rf"[/=_\-]{y}[/\-_]?0?{m}/?(?:$|[?&#])")
        for _u, h in pages:
            found = None
            for href in re.findall(r"href=[\"']([^\"'#]+)", h or ""):
                full = urljoin(_u, href.replace("&amp;", "&"))
                if urlparse(full).netloc == host and pat.search(full):
                    found = full
                    break
            if found and found.rstrip("/") not in have and found not in out:
                out.append(found)
                break
    return out


# 公式サイトに貼られている公開カレンダー。予定表をJSで描くサイトからLLMで
# 読み取るより、カレンダーそのものを読むほうが確実で無料枠も使わない。
_CALENDAR_LINK_RE = re.compile(
    r"https?://(?:timetreeapp\.com/public_calendars/|timetr\.ee/p(?:c)?/)([A-Za-z0-9_.\-]+)"
    r"|https?://calendar\.google\.com/calendar/(?:embed|u/0)?[^\"'\s<>]*",
    re.IGNORECASE)


# サイトの各予定に付いている「Googleカレンダーに追加」リンク。URL に公演名と
# 日時がそのまま入っているので、LLM を使わずに予定を取り出せる
# (ハロー！プロジェクトの各グループのページがこの作りになっている)。
_TEMPLATE_LINK_RE = re.compile(
    r"https?://(?:www\.)?calendar\.google\.com/calendar/[^\"'\s<>]*action=TEMPLATE[^\"'\s<>]*",
    re.IGNORECASE)
# TV・ラジオ・配信・雑誌などの露出は予定として扱わない(抽出プロンプトと同じ方針)
_MEDIA_TITLE_RE = re.compile(
    r"(ラジオ|radio|テレビ|ＴＶ|\bTV\b|放送|番組|YouTube|配信|生放送|ニコ生|SHOWROOM|"
    r"雑誌|連載|掲載|発売日|オンエア|on\s*air|FM|チャン[「『])", re.IGNORECASE)
# 放送局の略称+番組名(「MBS「西乃風ブラン堂」」など)。グループ名が英字のワンマン
# 公演(「PIGGS「FINAL LIVE」」)を巻き込まないよう、局名は決め打ちで並べる。
_BROADCASTER_RE = re.compile(
    r"^(?:NHK|MBS|TBS|ABC|CBC|KBC|RKB|YBS|STV|OBS|HBC|RSK|TSC|BSS|SBS|TVQ|TVO|"
    r"BS\s?\S{0,4}|CS\s?\S{0,4}|AT-X|Abema\S*|Hulu|Netflix|U-NEXT)[\s「『]",
    re.IGNORECASE)


def _is_media_title(title: str) -> bool:
    return bool(_MEDIA_TITLE_RE.search(title) or _BROADCASTER_RE.search(title))


def template_events(html: str, today: str) -> List["SiteEvent"]:
    """「Googleカレンダーに追加」リンクから今後の予定を組み立てる。

    recur=RRULE が付くもの(毎週のレギュラー番組など)と、TV/ラジオ/配信の類は
    予定として扱わない。
    """
    out, seen = [], set()
    for m in _TEMPLATE_LINK_RE.finditer(html or ""):
        u = m.group(0).replace("&amp;", "&")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        if q.get("recur"):
            continue
        title = (q.get("text") or [""])[0].strip()
        dates = (q.get("dates") or [""])[0]
        dm = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?", dates)
        if not title or not dm:
            continue
        if _is_media_title(title):
            continue
        day = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
        if day < today:
            continue
        when = f"{day} {dm.group(4)}:{dm.group(5)}" if dm.group(4) else day
        key = (title, when)
        if key in seen:
            continue
        seen.add(key)
        out.append(SiteEvent(event_name=title, event_datetime=when))
    return out


# 予定一覧の種類タグのうち、会場に行く予定を表すもの(RADIO・TV・MAGAZINE・RELEASE などは除く)
_SCHEDULE_LIVE_TAGS = {"LIVE", "EVENT", "TOUR", "FES", "FESTIVAL", "STAGE", "CONCERT",
                       "ライブ", "イベント", "ツアー", "特典会", "舞台", "公演"}


def _cls(el) -> str:
    return " ".join(el.get("class") or []) + " " + (el.get("id") or "")


_SCHEDULE_CONTEXT = re.compile(r"schedule|calendar", re.I)
_TAG_CLASS = re.compile(r"(^|[\s_\-])(tags?|category|cgy|genre|label)([\s_\-]|$)", re.I)
_DATE_CLASS = re.compile(r"date|time", re.I)
_TITLE_CLASS = re.compile(r"title|text|name|ttl", re.I)
_PLACE_CLASS = re.compile(r"location|place|venue", re.I)
_YMD = re.compile(r"(20\d\d)\s*[./\-年]\s*(\d{1,2})\s*[./\-月]\s*(\d{1,2})")


def schedule_list_events(html: str, today: str, page_url: str = "") -> List["SiteEvent"]:
    """予定一覧の決まった形から、LLM を使わずに今後の公演を取り出す。

    公式サイトの予定一覧は、1件ずつ「種類のタグ(LIVE/EVENT/MEDIA…)・日付・公演名
    (・会場)」が並ぶ作りが多い。LLM はこの一覧から公演を落とすことがある
    (ばってん少女隊の 11/3「MATSURI-10」in 東京、虹のコンキスタドールの 11/2
    「にじいろプリンセスパーティー！」)。
      ばってん少女隊など: li.contents-list-item > .schedule-tags .tag + time[datetime] + .title
      虹のコンキスタドール: li.top-schedule__item > .top-schedule__tag + __date + __text
    ニュース一覧も同じ形をしていて、日付は投稿日なので使えない。一覧かその親の
    クラス名に schedule・calendar が付いているものだけを読む。
    種類が LIVE・EVENT などのものだけを採る(RADIO・TV・MAGAZINE などは除く)。
    """
    if not html or not _SCHEDULE_CONTEXT.search(html):
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for li in soup.find_all("li"):
        if li.find("li"):
            continue   # 一覧の入れ物
        ctx = [li] + list(li.parents)[:4]
        if not any(_SCHEDULE_CONTEXT.search(_cls(e)) for e in ctx) and not li.select('[class*="schedule"]'):
            continue
        tags = set()
        for el in li.find_all(True):
            if _TAG_CLASS.search(_cls(el)) and not el.find(True, class_=_TAG_CLASS):
                for t in re.split(r"[/_・\s]+", el.get_text(" ", strip=True).upper()):
                    if t:
                        tags.add(t)
        if not tags & _SCHEDULE_LIVE_TAGS:
            continue
        day = None
        t = li.find("time", attrs={"datetime": True})
        if t and re.match(r"\d{4}-\d{2}-\d{2}", t["datetime"]):
            day = t["datetime"][:10]
        else:
            for el in li.find_all(True, class_=_DATE_CLASS):
                m = _YMD.search(el.get_text(" ", strip=True))
                if m:
                    day = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    break
        title = next((el for el in li.find_all(True)
                      if _TITLE_CLASS.search(_cls(el)) and not _TAG_CLASS.search(_cls(el))
                      and not _DATE_CLASS.search(_cls(el))), None)
        if not day or not title or day < today:
            continue
        name = re.sub(r"\s+", " ", title.get_text(" ", strip=True))
        if len(name) < 4 or _is_media_title(name) or (day, name) in seen:
            continue
        seen.add((day, name))
        place = next((el for el in li.find_all(True) if _PLACE_CLASS.search(_cls(el))), None)
        link = li.find("a", href=True) or (li.parent.find_parent("a", href=True) if li.parent else None)
        out.append(SiteEvent(
            event_name=name,
            event_datetime=day,
            venue=re.sub(r"\s+", " ", place.get_text(" ", strip=True)) or None if place else None,
            source_url=urljoin(page_url, link["href"]) if link and page_url else None,
        ))
    return out


# 公演ごとの詳細ページを探すためのリンク。イベント名に一致するリンク文字列が
# あれば、そのURLを案内先にできる(サイトのトップに飛ばすより親切)。
_ANCHOR_MIN_TEXT = 6


def page_anchors(html: str, base_url: str):
    """ページ内の同一ドメインのリンクを [(リンク文字列, URL)] で返す。

    ヘッダ・フッタ・ナビのリンクは公演名と関係ないので除く。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["header", "footer", "nav", "script", "style"]):
        tag.decompose()
    base_host = urlparse(base_url).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text() or "").strip()
        if len(text) < _ANCHOR_MIN_TEXT:
            continue
        full = urljoin(base_url, a["href"]).split("#", 1)[0]
        if urlparse(full).netloc != base_host or _NOT_A_PAGE.search(urlparse(full).path):
            continue
        key = (text, full)
        if key in seen:
            continue
        seen.add(key)
        out.append((text, full))
    return out


_BLOCK_TAGS = {"li", "article", "tr", "dl", "section"}
# 公演の案内ではないページ(写真・ブログ・作品・グッズ・プロフィールなど)
_NOT_EVENT_PAGE = re.compile(
    r"photo|gallery|blog|diary|discography|goods|shop|store|movie|video|profile|member|fanclub|"
    r"/fc/|login|mypage|contact|privacy|faq", re.I)
_MD_IN_TEXT = re.compile(r"(?<![\d/.])(\d{1,2})\s*[./月]\s*(\d{1,2})(?![\d/.])")
# サイトごとの「日付付きのリンク」。fetch_site_content が作り、取り込み側が公演ページ探しに使う
DATED_ANCHORS: dict = {}


def _dates_of(text: str, today) -> List[str]:
    """文字列中の日付(YYYY-MM-DD)。年が無ければ今日に近い年で補う。"""
    import datetime as _dt
    out = []
    for m in _YMD.finditer(text):
        try:
            out.append(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat())
        except ValueError:
            pass
    if out:
        return out
    for m in _MD_IN_TEXT.finditer(text):
        try:
            d = _dt.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if d < today and (today - d).days > YEARLESS_PAST_DAYS:
            d = d.replace(year=today.year + 1)
        out.append(d.isoformat())
    return out


def dated_anchors(html: str, base_url: str):
    """予定の項目からのリンクを [(日付, 項目の文字列, URL)] で返す。

    予定表は「日付・種類・公演名」がひとかたまりの項目になっていて、そこから公演ごとの
    詳細ページへリンクしていることが多い。リンク文字列が「詳細」「MORE」だけだったり、
    公演名の書き方が抽出結果と少し違ったりしても、同じ日付の項目なら同じ公演とわかる。
    """
    import datetime as _dt
    today = _dt.date.fromisoformat(_today_jst())
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["header", "footer", "nav", "script", "style"]):
        tag.decompose()
    base_host = urlparse(base_url).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"]).split("#", 1)[0]
        if urlparse(full).netloc != base_host or _NOT_A_PAGE.search(urlparse(full).path):
            continue
        path = urlparse(full).path
        if full.rstrip("/") == base_url.rstrip("/") or path.strip("/") == "" or _NOT_EVENT_PAGE.search(path):
            continue
        # リンクそのもの、無ければリンクを含む項目(li・article など)の文字列から日付を読む
        block = a
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        days = _dates_of(text, today)
        own = bool(days)   # 日付がリンク自体に書かれている(=予定の項目そのもの)
        if not days:
            for parent in list(a.parents)[:4]:
                if parent.name in _BLOCK_TAGS:
                    block = parent
                    break
            if block is not a:
                text = re.sub(r"\s+", " ", block.get_text(" ", strip=True))[:300]
                days = _dates_of(text, today)
        # 1つの項目に日付が並ぶ(期間・受付日など)ものは、どの日の公演か決められない
        if len(set(days)) != 1:
            continue
        key = (days[0], full)
        if key in seen:
            continue
        seen.add(key)
        out.append((days[0], text[:200], full, own))
    return out


def calendar_links(html: str):
    """HTML 中の TimeTree / Google カレンダーのURLを重複なく返す。"""
    out = []
    for m in _CALENDAR_LINK_RE.finditer(html or ""):
        u = m.group(0).replace("&amp;", "&")
        if u not in out:
            out.append(u)
    return out


def fetch_site_content(site_url: str):
    """公式サイト(+ News/Schedule サブページ)を取得し (content_hash, combined_text, jsonld_events)
    を返す(併せて、ページに貼られている公開カレンダーのURL、本文に付けた
    ページ番号→URLの対応、ページ内のリンク一覧も返す。ページ番号は LLM が返す
    source_page の解決に、リンク一覧は公演ごとの詳細ページ探しに使う)。
    取得できなければ (None, '', [], [], [], [])。**Geminiは呼ばない**(呼び出し側で content_hash を見て
    変化があるときだけ extract_events を呼ぶことで、無料枠の消費を最小化する)。
    静的HTMLの可視テキストが極端に短い(JS描画のSPA)場合は、ヘッドレスChromiumで
    描画し直す(いぎなり東北産・RE-GE のような予定表がJSで描画されるサイト対策)。
    併せて生HTML中の schema.org Event(JSON-LD)を Gemini不要で抽出して返す。"""
    import hashlib

    home = _get(site_url)
    home_text = _html_to_text(home) if home else ""
    # (URL, HTML) の組で持つ。どのページに書かれていた公演かを行に残すため
    # (チケットURLが無い公演の案内先を、サイトのトップではなくそのページにする)。
    pages: List[tuple] = []

    if len(home_text) < RENDER_MIN_CHARS:
        # SPAの可能性 → ヘッドレスで取り直す。描画で本文が増えればそれ(生HTML)を使う。
        rendered = _render_site(site_url)
        rendered_text_len = sum(len(_html_to_text(h)) for _u, h in rendered)
        if rendered_text_len > len(home_text):
            print(f"    JS描画で取得(静的{len(home_text)}字→描画{rendered_text_len}字)")
            pages = rendered
        else:
            if home:
                pages.append((site_url, home))
            for sub in (_find_subpages(home, site_url) if home else []):
                h = _fetch_subpage(sub)
                if h:
                    pages.append((sub, h))
    else:
        pages = [(site_url, home)]
        for sub in _find_subpages(home, site_url):
            h = _fetch_subpage(sub)
            if h:
                pages.append((sub, h))

    # 予定表が月ごとに分かれていれば、翌月・翌々月のページも読む
    for nxt in _next_month_pages(pages, site_url):
        h = _fetch_subpage(nxt)
        if h:
            print(f"    翌月以降の予定表も読みます: {nxt}")
            pages.append((nxt, h))

    texts = []
    page_tags = []   # [(P番号, URL)] 本文に出した見出しと実URLの対応
    for u, h in pages:
        t = _html_to_text(h)
        if not t:
            continue
        # 特設ページは「DATE／TIME / 2026年12月19日(土)」のように日付だけが並び、
        # 公演名はページ見出しにしか無いことがある。どのページの記述かを示す行を
        # 付けておくと、名前の無い日付のかたまりにも公演名を与えられる。
        # URL も添えて、公演ごとの案内先をLLMに選ばせる。
        t = drop_empty_date_lines(stitch_calendar_days(stitch_split_dates(t)))
        # 本文に渡すのは見出しだけにする。ページ番号やURLを足して公演ごとの案内先を
        # LLM に答えさせていたが、項目が増えた分だけ出力が長くなり、上限で打ち切られて
        # 取れる公演が激減した(いぎなり東北産が13件→2件)。案内先はリンク文字列と
        # 公演名の突合(LLM不要)で解決できるので、プロンプトは元の形に戻す。
        title = _page_title(h)
        page_tags.append((f"P{len(texts) + 1}", u))
        texts.append(f"{PAGE_MARK}{title}\n{t}" if title else t)

    # JSON-LD(schema.org Event)を抽出。名前+日時で重複除去。
    jsonld_events: List[SiteEvent] = []
    seen_ld = set()
    today_ld = _today_jst()
    for u, h in pages:
        for ev in (list(_parse_jsonld_events(h)) + template_events(h, today_ld)
                   + schedule_list_events(h, today_ld, u)):
            k = (ev.event_name, ev.event_datetime)
            if k not in seen_ld:
                seen_ld.add(k)
                if not ev.source_url:
                    ev.source_url = u
                jsonld_events.append(ev)
    if jsonld_events:
        print(f"    構造化データ(JSON-LD/カレンダー追加リンク)から "
              f"{len(jsonld_events)} 件のイベントを検出")

    cal_links = []
    for _u, h in pages:
        for u2 in calendar_links(h):
            if u2 not in cal_links:
                cal_links.append(u2)

    anchors = []
    dated = []
    for u, h in pages:
        for pair in page_anchors(h, u):
            if pair not in anchors:
                anchors.append(pair)
        for item in dated_anchors(h, u):
            if item not in dated:
                dated.append(item)
    DATED_ANCHORS[site_url] = dated
    page_urls = page_tags
    if not texts and not jsonld_events:
        return None, "", [], cal_links, page_urls, anchors
    combined = "\n\n".join(texts)
    # PROMPT_VERSION と JSON-LD署名を含めることで、本文/構造化データが変わると再抽出が走る。
    # JS描画で本文が変われば content_hash も変わるため、既読SPAサイトも自動で再抽出される。
    # どのページを材料にしたか / 凝縮後に日付がいくつ残るかは、0件だったときの
    # 切り分けに必要(ページは取れているのか、日付が落ちているのかが分かる)。
    heads = [ln[len(PAGE_MARK):][:28] for ln in combined.split("\n") if ln.startswith(PAGE_MARK)]
    cond = _condense_for_llm(combined)
    print(f"    素材 {len(texts)} ページ / 本文 {len(combined)} 字 → 凝縮 {len(cond)} 字 / "
          f"日付 {len(_DATE_IN_TEXT.findall(cond))} 件"
          + (f" / ページ: {' | '.join(heads[:4])}" if heads else ""))
    ld_sig = "|".join(f"{e.event_name}@{e.event_datetime}" for e in jsonld_events)
    content_hash = hashlib.sha1(
        (PROMPT_VERSION + "\n" + combined + "\n" + ld_sig).encode("utf-8", "ignore")
    ).hexdigest()
    return content_hash, combined, jsonld_events, cal_links, page_urls, anchors


# 年の無い日付が今年のこの日数以内の過去なら、来年ではなく終わった日と見る
YEARLESS_PAST_DAYS = 120


def has_future_dates(text: str) -> bool:
    """本文に今日以降(1年以内)の日付が含まれるか。

    抽出が 0 件で返ったとき、その素材に今後の日付すら無いなら「本当に予定が無い」。
    日付があるのに 0 件なら取りこぼしを疑い、呼び出し側で再試行の判断に使う。
    """
    import datetime as _dt

    if not text:
        return False
    today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).date()
    limit = today + _dt.timedelta(days=365)
    full = r"(20\d\d)[/年.\-](\d{1,2})[/月.\-](\d{1,2})"
    for m in re.finditer(full, text):
        try:
            d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if today <= d <= limit:
            return True
    # 年を伴わない「11/14」「8月20日」「10.05」等。年跨ぎを考え今年と翌年で判定する。
    # 年付きの日付は上で見たので外す。残すと「2026.10.05」の「10.05」を来年の
    # 10/5 と読み、終わった日を今後の日付としてしまう(過ぎた日の予定が優先され、
    # 翌月の公演が字数の枠から押し出されていた)。
    # 今年の日付として少し前(YEARLESS_PAST_DAYS 日以内)なら、終わった日と見る。
    # 来年の同じ日と読むと、月ごとの予定表で先月・今月前半の公演(「10/2（金）」)が
    # すべて今後の日付になり、翌月の公演が字数の枠から押し出される(踊れ！神風)。
    # 年明けの予定(11月に書かれた「1/10」)は、今年としては遠い過去なので来年と読む。
    for m in re.finditer(r"(?<!\d)(\d{1,2})[/月.](\d{1,2})日?(?!\d)", re.sub(full, " ", text)):
        try:
            d = _dt.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if d < today and (today - d).days > YEARLESS_PAST_DAYS:
            try:
                d = _dt.date(today.year + 1, d.month, d.day)
            except ValueError:
                continue
        if today <= d <= limit:
            return True
    return False
