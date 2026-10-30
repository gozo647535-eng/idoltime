"""IdolLife 取り込みワーカー

主軸(既定): 図鑑に登録したアイドルの公式サイトを巡回し、チケット/イベント情報を
Gemini で抽出して Supabase の auto_events テーブルへ書き込む(sites.py)。X より壊れにくい。

補助(任意): 環境変数 ENABLE_X_INGEST=1 のときのみ、X(twikit)からの取り込みも実行する。
※twikit は 2026/3 の X 変更以降不安定なため既定では無効。使う場合は requirements に twikit を戻す。

GitHub Actions から定期実行される想定。既読位置(watermark)は Actions が揮発するため
Supabase の worker_state テーブルに保存する。
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import datetime
import hashlib
import json
import re
import unicodedata
import traceback
import urllib.parse
from typing import List, Optional
from pydantic import BaseModel, Field

import sb  # Supabase 入出力(標準ライブラリのみ)
import sites  # 公式サイトからの取得エンジン(主軸)
import eplus  # イープラス(e+)からの取得エンジン
import gcal  # Google 公開カレンダー(ICS)からの取得エンジン
import lineup  # 出演欄から図鑑の登録グループを突合
import timetree
import notetime
import venuetext  # TimeTree 公開カレンダーからの取得エンジン
import ticketdive  # TicketDive イベントページの構造化パーサ(チケット情報の高精度化)
import ticketsite  # LivePocket / TiGET イベントページの構造化パーサ
import release  # 店舗のリリースイベント(タワレコ/HMV/ヴィレヴァン)
import yahoo_rt  # Yahoo!リアルタイム検索からのチケットサイト・イベント発見
from watchlist import (
    build_idol_name_list,
    build_gcal_watch_list,
    build_site_watch_list,
    build_timetree_watch_list,
    build_watch_list,
    site_skip_slugs,
)

COOKIES_FILE = "cookies.json"

# JST(アイドルのイベント時刻は日本時間前提)
JST = datetime.timezone(datetime.timedelta(hours=9))

# --- Gemini 準備 ---
gemini_client = None
gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
if gemini_api_key:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=gemini_api_key)
        print("Gemini API Client initialized.")
    except Exception as e:
        print(f"Gemini 初期化失敗。正規表現にフォールバックします: {e}")

# --- Groq 準備(公式サイト抽出の一次エンジン)---
# Groq は無料枠が日次で桁違いに大きく、Gemini の日次上限で監視サイト後半に届かない
# 「枠到達点問題」を回避できる。GROQ_API_KEY があれば sites 抽出で優先的に使う。
groq_client = None
groq_api_key = os.environ.get("GROQ_API_KEY", "")
if groq_api_key:
    try:
        from openai import OpenAI
        groq_client = OpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1")
        print(f"Groq (OpenAI互換) クライアントを初期化しました。model候補={sites._GROQ_MODELS}")
    except Exception as e:
        print(f"Groq 初期化失敗。Gemini にフォールバックします: {e}")


class TicketType(BaseModel):
    name: str = Field(description="券種の名前 (例: 一般, SS席, 前売り)")
    price: str = Field(description="料金 (例: 5000円, ¥3,500)")


class ParsedTicketInfo(BaseModel):
    is_ticket_info: bool = Field(description="チケットの発売・受付・先行・販売に関する具体的な投稿なら True。単なる告知や日常ツイートは False。")
    is_first_come: bool = Field(description="販売方法が『先着』『一般発売(先着順)』なら True。抽選・先行(抽選)・情報なしは False。")
    event_name: Optional[str] = Field(None, description="イベント名または公演名")
    event_datetime: Optional[str] = Field(None, description="イベントの開催日時 (例: 2026/06/20 18:00)")
    sale_datetime: Optional[str] = Field(None, description="チケット発売/受付日時。必ず 'YYYY-MM-DD HH:MM:SS' 形式。年が不明なら現在の年を補完、秒は 00。")
    max_tickets: Optional[str] = Field(None, description="最大申込可能枚数 (例: 4枚)")
    ticket_types: List[TicketType] = Field(default=[], description="券種と料金のリスト")
    ticket_url: Optional[str] = Field(None, description="チケット販売ページ等のURL")
    other_details: Optional[str] = Field(None, description="その他の詳細(会場名や注意事項等)")


x_client = None  # X取り込み有効時に run_x() で遅延生成

# チケット販売サイトのドメイン断片(ツイート内リンクの判定に使用)
# Ticketdive / e+(イープラス) / チケットぴあ / ローソンチケット / LivePocket / tiget(チゲット) など
TICKET_DOMAINS = [
    "ticket", "dive", "t-dv.com",          # Ticketdive
    "eplus", "eplus.jp",                     # イープラス
    "pia", "t.pia.jp",                       # チケットぴあ
    "l-tike", "l-tike.com",                  # ローソンチケット
    "livepocket", "t.livepocket",            # LivePocket
    "tiget", "tiget.net",                    # チゲット
]


async def login_x() -> bool:
    if os.path.exists(COOKIES_FILE):
        try:
            x_client.load_cookies(COOKIES_FILE)
            print("セッションクッキーのロードに成功。")
            return True
        except Exception as e:
            print(f"クッキーのロードに失敗: {e}")
    # フォールバック: 認証情報からログイン(Secrets 経由)
    username = os.environ.get("X_USERNAME")
    if not username:
        print("X のログイン情報(cookies.json または X_USERNAME)がありません。")
        return False
    try:
        await x_client.login(
            auth_info_1=username,
            auth_info_2=os.environ.get("X_EMAIL"),
            password=os.environ.get("X_PASSWORD"),
        )
        x_client.save_cookies(COOKIES_FILE)
        print("ログイン成功、cookies.json を保存。")
        return True
    except Exception as e:
        print(f"X ログイン失敗: {e}")
        return False


_FULLWIDTH = str.maketrans("０１２３４５６７８９：／－．", "0123456789:/-.")


def _zen2han(s: Optional[str]) -> Optional[str]:
    """全角数字・コロンを半角へ。日時パースの取りこぼしを減らす。"""
    return s.translate(_FULLWIDTH) if s else s


def normalize_datetime_string(raw_text: str) -> Optional[str]:
    if not raw_text:
        return None
    try:
        t = _zen2han(raw_text)
        # YYYY(年/-/.) MM DD [HH:MM](分・区切りは緩め。「時」表記や分省略にも対応)
        m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})[日\s]*[^\d\s]*\s*(\d{1,2})[：:時](\d{1,2})?", t)
        if m:
            y, mo, d, h, mi = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(h):02d}:{int(mi or 0):02d}:00"
        # 年なし MM/DD [HH:MM] → 当年補完 + 年跨ぎ対策(過去すぎれば翌年)
        m = re.search(r"(\d{1,2})[-/月.](\d{1,2})[日\s]*[^\d\s]*\s*(\d{1,2})[：:時](\d{1,2})?", t)
        if m:
            mo, d, h, mi = m.groups()
            today = datetime.datetime.now(JST).date()
            try:
                cand = datetime.date(today.year, int(mo), int(d))
            except ValueError:
                return None
            if cand < today - datetime.timedelta(days=60):
                cand = cand.replace(year=today.year + 1)
            return f"{cand.isoformat()} {int(h):02d}:{int(mi or 0):02d}:00"
    except Exception as e:
        print(f"normalize error '{raw_text}': {e}")
    return None


def normalize_event_datetime(raw: Optional[str]) -> Optional[str]:
    """公演日時を 'YYYY-MM-DD' か 'YYYY-MM-DD HH:MM' に正規化(表示・重複判定の一貫性用)。
    日付が取れなければ原文のまま返す(情報を捨てない)。"""
    if not raw:
        return raw
    t = _zen2han(raw)
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", t)
    if m:
        y, mo, d = m.groups()
        base = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    else:
        # 年なし MM/DD → 当年補完(過去すぎれば翌年)
        m = re.search(r"(?<!\d)(\d{1,2})[-/月.](\d{1,2})(?!\d)", t)
        if not m:
            return raw
        mo, d = m.groups()
        today = datetime.datetime.now(JST).date()
        try:
            cand = datetime.date(today.year, int(mo), int(d))
        except ValueError:
            return raw
        if cand < today - datetime.timedelta(days=60):
            cand = cand.replace(year=today.year + 1)
        base = cand.isoformat()
    tm = re.search(r"(\d{1,2})[：:時](\d{2})", t)
    return f"{base} {int(tm.group(1)):02d}:{tm.group(2)}" if tm else base


def clean_venue(v: Optional[str]) -> Optional[str]:
    """会場名の整形。前後の空白・改行を詰め、『会場:』『@』等の接頭ラベルを落とす。"""
    if not v:
        return v
    v = re.sub(r"\s+", " ", v).strip()
    v = re.sub(r"^(?:会場|場所|at|＠|@)\s*[:：]?\s*", "", v, flags=re.IGNORECASE)
    return v.strip() or None


def stage_time_of(ev: dict) -> Optional[str]:
    """そのグループの出演時刻。メモに「出演19:35-20:00」と書かれていれば取れる。

    どのソースも構造化された出演時刻を持たないが、グループ自身のカレンダーの
    メモには書かれていることが多い。メモはそのカレンダーの持ち主のものなので、
    ここで取れる時刻はそのグループの出演時刻として扱える。
    他グループへ相互反映した行(ttx:)には使わない(持ち主が違う)。
    """
    return notetime.stage_time_from_note(ev.get("note"))


def venue_of(ev: dict) -> Optional[str]:
    """予定の会場名。会場欄が空ならメモから拾う。

    TimeTree・Googleカレンダーの会場欄(location)は空のことが多く(実測で
    TimeTree 52%・Googleカレンダー 90%)、代わりにメモへ「会場：〇〇」「📍〇〇」と
    書かれている。会場名が無いと都道府県での絞り込みも会場ページも働かないので拾う。
    """
    v = clean_venue(ev.get("venue"))
    if v:
        return v
    return clean_venue(venuetext.venue_from_note(ev.get("note"), ev.get("title")))


def parse_ticket_info_fallback(text: str, urls: Optional[List[str]] = None) -> dict:
    info = {
        "is_ticket_info": False, "is_first_come": False, "event_name": None,
        "event_datetime": None, "sale_datetime": None, "max_tickets": None,
        "ticket_types": [], "ticket_url": None, "other_details": None,
    }
    ticket_urls = [u for u in (urls or []) if any(k in u for k in TICKET_DOMAINS)]
    has_kw = any(k in text for k in ["チケット", "受付", "発売", "販売", "先行", "一般発売", "抽選", "一般販売", "先着"])
    if ticket_urls:
        info["ticket_url"] = ticket_urls[0]
    elif urls and has_kw:
        info["ticket_url"] = urls[0]

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        if not any(k in line for k in ["日時", "発売", "受付", "料金", "チケット", "価格", "http", "枚", "限定", "詳細", "【", "★", "◆"]):
            info["event_name"] = line
            break
    for line in lines:
        if line.startswith("【") and line.endswith("】") and len(line) < 10:
            continue
        if any(k in line for k in ["日時", "開催", "公演"]) and not info["event_datetime"]:
            if re.search(r"\d", line):
                info["event_datetime"] = line
        elif any(k in line for k in ["発売", "受付", "販売", "リリース"]) and not info["sale_datetime"]:
            if re.search(r"\d", line):
                info["sale_datetime"] = normalize_datetime_string(line) or line
        elif any(k in line for k in ["枚数", "枚制限", "枚まで", "限"]) and not info["max_tickets"]:
            info["max_tickets"] = line
        elif any(k in line for k in ["円", "￥", "¥", "料金", "価格"]):
            if line in ["料金：", "料金:", "チケット代：", "チケット代:", "価格：", "価格:"]:
                continue
            parts = line.split(":") if ":" in line else line.split("：")
            name, price = (parts[0].strip(), parts[1].strip()) if len(parts) > 1 else ("券種", line)
            info["ticket_types"].append({"name": name, "price": price})

    has_neg = any(k in text for k in ["申し訳", "お詫び", "払い戻し", "返金", "不具合", "システムエラー", "延期", "中止"])
    info["is_ticket_info"] = (has_kw or info["ticket_url"]) and not has_neg
    has_fc = any(k in text for k in ["先着", "一般発売", "一般販売"])
    has_lot = any(k in text for k in ["抽選", "先行予約", "先行受付", "プレオーダー"])
    info["is_first_come"] = has_fc and not has_lot
    return info


async def scrape_ticketdive_page(url: str) -> Optional[dict]:
    if not url or ("ticketdive.com/event/" not in url and "t-dv.com/" not in url):
        return None
    import aiohttp
    from bs4 import BeautifulSoup
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10, allow_redirects=True) as response:
                if response.status != 200:
                    return None
                final_url = str(response.url)
                if "ticketdive.com/event/" not in final_url:
                    return None
                html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.text.strip() if soup.title else ""
        title = title.replace(" | TicketDive", "")
        cards = soup.find_all(lambda t: t.name == "div" and any("Card_card" in c for c in t.get("class", [])))
        card_texts, max_tickets = [], 0
        for idx, card in enumerate(cards, 1):
            txt = re.sub(r"\s+", " ", card.get_text(" | ").strip())
            card_texts.append(f"[チケット情報 #{idx}] {txt}")
            for sel in card.find_all("select"):
                opts = [o.get("value") for o in sel.find_all("option") if o.get("value", "").isdigit()]
                if opts:
                    max_tickets = max(max_tickets, max(map(int, opts)))
        return {
            "title": title,
            "scraped_text": f"イベント名: {title}\n" + "\n".join(card_texts),
            "max_tickets": f"{max_tickets}枚" if max_tickets else None,
            "resolved_url": final_url,
        }
    except Exception as e:
        print(f"Ticketdive スクレイプ失敗: {e}")
        return None


async def parse_tweet(text, scraped_data=None, ticket_url=None, urls=None) -> dict:
    if gemini_client:
        if scraped_data:
            prompt = f"以下のツイートとチケット販売ページ情報を分析し、正確なJSONを出力してください。\n\n【ツイート本文】\n{text}\n\n【販売ページ(Ticketdive)】\nURL: {ticket_url}\n{scraped_data['scraped_text']}"
        else:
            prompt = f"以下のツイートからチケット販売情報を抽出し正確なJSONを出力してください。\n\n【ツイート本文】\n{text}"
        try:
            from google.genai import types
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ParsedTicketInfo,
                    temperature=0.1,
                ),
            )
            result = response.parsed.model_dump()
            if not result.get("ticket_url") and ticket_url:
                result["ticket_url"] = ticket_url
            if scraped_data and scraped_data.get("max_tickets") and not result.get("max_tickets"):
                result["max_tickets"] = scraped_data["max_tickets"]
            if scraped_data:
                result["is_ticket_info"] = True
            if result.get("sale_datetime"):
                result["sale_datetime"] = normalize_datetime_string(result["sale_datetime"]) or result["sale_datetime"]
            return result
        except Exception as e:
            print(f"Gemini 解析失敗、正規表現にフォールバック: {e}")
    info = parse_ticket_info_fallback(text, urls)
    if ticket_url:
        info["ticket_url"] = ticket_url
    if scraped_data:
        info["is_ticket_info"] = True
        if scraped_data.get("title") and not info.get("event_name"):
            info["event_name"] = scraped_data["title"]
        if scraped_data.get("max_tickets") and not info.get("max_tickets"):
            info["max_tickets"] = scraped_data["max_tickets"]
    if info.get("sale_datetime"):
        info["sale_datetime"] = normalize_datetime_string(info["sale_datetime"]) or info["sale_datetime"]
    return info


def to_timestamptz(sale: Optional[str]) -> Optional[str]:
    """'YYYY-MM-DD HH:MM:SS'(JST) → ISO+09:00。パースできなければ None。"""
    if not sale:
        return None
    try:
        dt = datetime.datetime.strptime(sale, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=JST).isoformat()
    except Exception:
        return None


# 同一実行内で同じ TicketDive ページを何度も取得しないための簡易キャッシュ
_td_cache: dict = {}


def _td_info(url):
    if url in _td_cache:
        return _td_cache[url]
    try:
        info = ticketdive.fetch_ticketdive(url)
    except Exception as e:
        print(f"    TicketDive 取得失敗: {e}")
        info = None
    _td_cache[url] = info
    return info


_UNDECIDED_RE = re.compile(r"[（(][^）)]*未定[^）)]*[）)]")


def expand_timetree_row(row: dict, idol_name: str):
    """ticket_url が複数日開催の TicketDive フェスなら、そのアイドルが実際に出演する
    日ごとの行に分割する(出演日をTicketDiveの日別ラインナップから確定)。
    単日 or マッチしなければ [row] のまま。"""
    url = row.get("ticket_url")
    if not ticketdive.is_ticketdive_url(url):
        return [row]
    info = _td_info(url)
    if not info:
        return [row]
    day_stages = info.get("day_stages") or []
    if len({d["date"] for d in day_stages}) <= 1:  # 単日イベント
        return [row]
    days = ticketdive.match_idol_days(day_stages, idol_name)
    if not days:  # ラインナップに見つからなければ元の行を維持
        return [row]
    fk = ticketdive.festival_key(url)
    name = info.get("event_name") or _UNDECIDED_RE.sub("", row.get("event_name") or "").strip()
    # ツアーは日付ごとに会場が違うので、日付に対応する会場を入れる
    venues = ticketdive.venue_by_date(day_stages)
    opens = ticketdive.open_time_by_date(day_stages)
    out = []
    for dt in days:
        r = dict(row)
        r["ticket_types"] = list(row.get("ticket_types") or [])
        r["event_name"] = name
        r["event_datetime"] = dt
        if venues.get(dt):
            r["other_details"] = clean_venue(venues[dt])
        if opens.get(dt):
            r["open_time"] = opens[dt]
        # フェス識別子+日付でIDを作る → 同一フェスの複数TimeTree投稿でも1日1行に集約
        r["tweet_id"] = f"tt:{row['idol_slug']}:tdf:{fk}:{dt.replace('-', '')}"
        out.append(r)
    return out


def enrich_row_with_ticketdive(row: dict) -> dict:
    """row の ticket_url が TicketDive なら、構造化データで発売日時・先着・券種・会場を
    補完/上書きする(TicketDive は販売元なので発売情報は最も正確・Gemini不要)。"""
    url = row.get("ticket_url")
    if not ticketdive.is_ticketdive_url(url):
        return row
    info = _td_info(url)
    if not info:
        return row
    # 名前が汎用的("ライブ予定"等)/空なら販売元の正式名で上書き
    if info.get("event_name") and (
        not row.get("event_name") or timetree.looks_placeholder(row["event_name"])
    ):
        row["event_name"] = info["event_name"]
    if info.get("sale_datetime"):  # 販売元の日時を優先
        row["sale_datetime"] = to_timestamptz(info["sale_datetime"])
        row["is_first_come"] = bool(info.get("is_first_come"))
    if info.get("ticket_types") and not row.get("ticket_types"):
        row["ticket_types"] = info["ticket_types"]
    if not row.get("event_datetime") and info.get("event_datetime"):
        row["event_datetime"] = info["event_datetime"]
    if not row.get("open_time") and info.get("open_time"):
        row["open_time"] = info["open_time"]
    if not row.get("other_details") and info.get("venue"):
        row["other_details"] = clean_venue(info["venue"])
    return row


def build_row(tweet, username: str, idol_slug: str, info: dict) -> dict:
    open_t, entry_t = timetree.extract_open_entry(tweet.text)
    return {
        "tweet_id": str(tweet.id),
        "idol_slug": idol_slug,
        "account": username,
        "event_name": info.get("event_name"),
        "event_datetime": normalize_event_datetime(info.get("event_datetime")),
        "open_time": open_t,
        "entry_time": entry_t,
        "sale_datetime": to_timestamptz(info.get("sale_datetime")),
        "is_first_come": bool(info.get("is_first_come")),
        "ticket_url": info.get("ticket_url"),
        "ticket_types": info.get("ticket_types") or [],
        "max_tickets": info.get("max_tickets"),
        "other_details": clean_venue(info.get("other_details")),
        "raw_text": tweet.text,
        "tweet_url": f"https://x.com/{username}/status/{tweet.id}",
    }


async def check_account(username: str, idol_slug: str) -> int:
    username = username.strip().replace("@", "")
    if not username:
        return 0
    print(f"@{username} を確認中...")
    try:
        user = await x_client.get_user_by_screen_name(username)
        tweets = await x_client.get_user_tweets(user.id, "Tweets")
    except Exception as e:
        print(f"@{username} の取得に失敗: {e}")
        return 0
    if not tweets:
        return 0

    last_id = sb.get_last_seen(username)
    if not last_id:
        # 初回はスパム防止のため最新IDだけ記録(通知は次回以降)
        sb.set_last_seen(username, tweets[0].id)
        print(f"@{username} 初期化。最新ID {tweets[0].id} を記録。")
        return 0

    new_tweets = []
    for t in tweets:
        if int(t.id) > int(last_id):
            new_tweets.append(t)
        else:
            break
    new_tweets.reverse()
    if not new_tweets:
        return 0
    print(f"@{username} に新規 {len(new_tweets)} 件。解析します。")

    processed = 0
    for t in new_tweets:
        try:
            urls = [u.get("expanded_url") for u in (t.urls or []) if u.get("expanded_url")]
            ticket_candidates = [u for u in urls if any(k in u for k in TICKET_DOMAINS)]
            ticket_url = ticket_candidates[0] if ticket_candidates else (urls[0] if urls else None)
            ticketdive_url = next((u for u in urls if "ticketdive.com/event/" in u or "t-dv.com/" in u), None)

            scraped = await scrape_ticketdive_page(ticketdive_url) if ticketdive_url else None
            if scraped and scraped.get("resolved_url"):
                ticket_url = scraped["resolved_url"]

            info = await parse_tweet(t.text, scraped, ticket_url, urls)
            if info.get("is_ticket_info"):
                sb.upsert_auto_event(build_row(t, username, idol_slug, info))
                print(f"  → auto_events に書き込み: {info.get('event_name') or '(名称不明)'}")
            else:
                print(f"  チケット情報ではないためスキップ: {t.id}")
        except Exception as e:
            print(f"  ツイート {t.id} の処理でエラー: {e}")
            traceback.print_exc()
        finally:
            sb.set_last_seen(username, t.id)
            processed += 1
            await asyncio.sleep(3.0)
    return processed


# 「先行受付」「一般発売」等は公演ではなくチケット情報。プロンプトでも禁じているが、
# モデルが取り違えることがあるので、ここでも落とす。
# ただし「発売記念イベント」「特典会」のように実際に客が集まる催しは残す。
_RECEPTION_ONLY = re.compile(r"(受付|先行発売|一般発売|プレイガイド|抽選申込|二次先行|チケット発売)")
_REAL_GATHERING = re.compile(r"(イベント|特典会|ライブ|LIVE|公演|フェス|生誕|ワンマン|ツアー|握手|お話し?会|撮影会)", re.IGNORECASE)


def _sane_sale_datetime(sale: Optional[str], event_dt: Optional[str], name: str) -> Optional[str]:
    """受付開始が公演日より後、というあり得ない値を落とす。

    LLM は受付の年を書き落とすことがあり(「7/26 10:00」→ 翌年に補完される等)、
    公演の数か月後に受付が始まる行ができていた。1年引いて辻褄が合うならそれを採り、
    合わなければ受付情報を捨てる(公演自体は残す)。
    """
    if not sale or not event_dt:
        return sale
    try:
        sd = datetime.datetime.fromisoformat(sale.replace("Z", "+00:00"))
        ed = datetime.datetime.fromisoformat(event_dt[:10])
    except ValueError:
        return sale
    if sd.date() <= ed.date():
        return sale
    # 年の取り違えは「受付が公演の半年以上あと」という形で現れる。少しだけ後ろに
    # ずれている場合は年ではなく日付そのものが怪しいので、直さず受付情報を捨てる。
    gap = (sd.date() - ed.date()).days
    shifted = sd.replace(year=sd.year - 1)
    if gap > 60 and shifted.date() <= ed.date():
        print(f"    受付日が公演日より後だったため1年戻しました: {name[:30]} {sd.date()} → {shifted.date()}")
        return shifted.isoformat()
    print(f"    受付日({sd.date()})が公演日({ed.date()})より後のため受付情報を外します: {name[:30]}")
    return None


def _event_anchor_url(name: str, anchors) -> Optional[str]:
    """公演名に一致するリンクを探して、そのURLを返す。

    サイトは公演ごとに詳細ページを持っていることが多く、一覧からは公演名の
    リンクで飛べる。リンク文字列と公演名のどちらかが他方を含んでいれば同じ公演と
    見なし、いちばん長く一致したものを採る(「詳細」「チケット」のような短い
    リンクは page_anchors の段階で除いてある)。
    """
    key = lineup._light(name or "")
    key = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]", "", key)
    if len(key) < 6:
        return None
    # ① 公演名をそのまま含むリンク → いちばん短いもの(余計な語が少ない=その公演の
    #    ページらしい。ニュース一覧の長い見出しリンクに引っ張られないようにする)
    # ② 無ければ、公演名に含まれるリンク文字列 → いちばん長いもの
    contains, contained = [], []
    for text, url in (anchors or []):
        t = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]", "", lineup._light(text))
        if len(t) < 6:
            continue
        if key in t:
            contains.append((len(t), url))
        elif t in key:
            contained.append((len(t), url))
    if contains:
        return min(contains)[1]
    if contained:
        return max(contained)[1]
    return None


# 一覧のページ(公演ごとのページではない)
_LIST_PAGE = re.compile(r"/(?:schedules?|calendar|vertical_calendar|news|live|lives|events?|info|information|media|contents|list)/?$"
                        r"|/(?:schedules?|calendar|vertical_calendar)/20\d\d(?:/\d{1,2})?/?$", re.I)
_URL_DAY = re.compile(r"(20\d\d)[-/_]?(\d{1,2})[-/_](\d{1,2})(?!\d)")


def _other_day(day: str, text: str, url: str) -> bool:
    """リンクの文字か URL に、公演日と違う日付だけが書かれているか。"""
    if not day:
        return False
    found = set()
    m = _URL_DAY.search(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    if m:
        try:
            found.add(datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat())
        except ValueError:
            pass
    if text:
        found.update(sites._dates_of(text, datetime.date.fromisoformat(day) - datetime.timedelta(days=30)))
    return bool(found) and day not in found


def _common_run(a: str, b: str) -> int:
    """2つの文字列に共通する、いちばん長い連続部分の長さ。"""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def _dated_anchor_url(ev, dated) -> Optional[str]:
    """同じ日付の予定の項目からのリンク(公演の詳細ページ)。

    公演名が4文字以上重なる項目を採る。重ならなくても、その日の項目が1つだけなら採る
    (リンク文字列が「詳細」だけ、抽出した公演名が略されている、などの場合)。
    """
    day = (normalize_event_datetime(getattr(ev, "event_datetime", None)) or "")[:10]
    if not day or not dated:
        return None
    same = [(t, u, own) for d, t, u, own in dated if d == day]
    if not same:
        return None
    key = _id_name(getattr(ev, "event_name", "") or "")
    scored = sorted(((_common_run(key, _id_name(t)), u) for t, u, _own in same), reverse=True)
    if key and scored[0][0] >= 4:
        return scored[0][1]
    # 名前が重ならないときは、日付がリンクそのものに書かれた項目(予定表の1件)が
    # その日に1つだけのときに限る。まわりの文字から日付を拾ったリンクは別の公演のことがある
    urls = {u for _t, u, own in same if own}
    return next(iter(urls)) if len(urls) == 1 and len({u for _t, u, _o in same}) == 1 else None


def _site_event_url(ev, site_url: str, page_tags=None, anchors=None) -> str:
    """公演の案内先URL。

    ① 構造化データが持っていた公演ページのURL(同じサイト内のときだけ採る)
    ② 公演名に一致するリンク(= その公演の詳細ページ)
    ③ LLM が答えたページ番号(P1 等)に対応するURL
    ④ どれも無ければサイトのトップ
    の順に選ぶ。チケットURLが無い公演はアプリからここへ案内する。
    """
    def host(u: str) -> str:
        return urllib.parse.urlparse(u).netloc.lower().removeprefix("www.")

    src = (getattr(ev, "source_url", None) or "").strip()
    if src.startswith("http"):
        try:
            if host(src) == host(site_url):
                return src
        except Exception:
            pass
    day = (normalize_event_datetime(getattr(ev, "event_datetime", None)) or "")[:10]
    # リンクの文字や URL に別の日付が書かれていれば、それは別の公演のページ
    # (FRUITS ZIPPER のツアーが、名前の重なる 11/1 の写真集イベントのページに、
    #  POEM の 11/3 が、サイト側の誤りで 11/1 のページに案内されていた)
    anchors = [(t, u) for t, u in (anchors or []) if not _other_day(day, t, u)]
    hit = _event_anchor_url(getattr(ev, "event_name", "") or "", anchors)
    if hit and hit.rstrip("/") != site_url.rstrip("/"):
        return hit
    dated = [x for x in (sites.DATED_ANCHORS.get(site_url) or []) if not _other_day(day, "", x[2])]
    hit = _dated_anchor_url(ev, dated)
    if hit:
        return hit
    # 特設ページ: ページの見出しが公演名と6文字以上重なるもの(一覧・ニュースのページは除く)
    key = _id_name(getattr(ev, "event_name", "") or "")
    best = (0, None)
    for title, u in sites.PAGE_TITLES.get(site_url) or []:
        if _LIST_PAGE.search(urllib.parse.urlparse(u).path) or _other_day(day, title, u):
            continue
        n = _common_run(key, _id_name(title))
        if n > best[0]:
            best = (n, u)
    if key and best[0] >= 6:
        return best[1]
    tag = (getattr(ev, "source_page", None) or "").strip().upper()
    if tag:
        for t, u in (page_tags or []):
            if t == tag:
                return u
    return site_url


# ページの見出しだけで、公演名になっていないもの
_GENERIC_HEADING = re.compile(
    r"^(?:schedule|news|event|live|ticket|tickets|info|information|home|top|more|view\s*more|"
    r"予定|お知らせ|ニュース|スケジュール|イベント|ライブ|チケット|トップ|一覧|公演情報|最新情報|"
    r"カレンダー|calendar)\W*$",
    re.IGNORECASE)


# 行IDに使う公演名。囲み記号・空白・全半角の違いを無視する
_ID_NAME_STRIP = re.compile(r"[\s　「」『』\"'“”‘’【】\[\]()（）〈〉《》!！?？・,、.。:：;；~〜\-–—_]+")


def _id_name(name) -> str:
    return _ID_NAME_STRIP.sub("", unicodedata.normalize("NFKC", name or "")).lower()


def build_site_row(slug: str, name: str, site_url: str, ev, page_tags=None,
                   anchors=None) -> Optional[dict]:
    """公式サイト由来の SiteEvent を auto_events 行に変換。tweet_id は合成した安定IDで重複排除。

    tweet_url は、その公演が書かれていたページ(source_url)を優先する。チケットURLが
    無い公演はアプリからここへ案内するので、サイトのトップより具体的なほうがよい。
    LLM には本文に付けたページ番号(P1 等)で答えさせ、こちらでURLに戻す。
    """
    if not ev.event_name:
        return None
    if _RECEPTION_ONLY.search(ev.event_name) and not _REAL_GATHERING.search(ev.event_name):
        print(f"  チケット受付のためイベントにしません: {ev.event_name[:40]}")
        return None
    if _GENERIC_HEADING.match(ev.event_name.strip()):
        # ページの見出し(「SCHEDULE」等)を公演名として拾ってしまった行。
        # 公演名が無いので、イベントとしては使えない。
        print(f"  ページ見出しのためイベントにしません: {ev.event_name[:40]}")
        return None
    sale = None
    if ev.sale_datetime:
        sale = to_timestamptz(normalize_datetime_string(ev.sale_datetime) or ev.sale_datetime)
    event_dt = normalize_event_datetime(ev.event_datetime)
    sale = _sane_sale_datetime(sale, event_dt, ev.event_name)
    # IDは公演名の表記揺れに強くする。LLM は同じ公演を回ごとに「…」/『…』と
    # 書き分けるため、生の文字列で作ると毎回別の行になり、古い行が残ったまま
    # 新しい行が増え続ける(超ときめき♡宣伝部で7件→13件になった)。
    # 発売日時も鍵に入れない(あとから発売日が決まっただけで別の行にしない)。
    key = hashlib.sha1(
        f"{slug}|{_id_name(ev.event_name)}|{(event_dt or '')[:10]}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "tweet_id": f"site:{slug}:{key}",
        "idol_slug": slug,
        "account": None,
        "event_name": ev.event_name,
        "event_datetime": event_dt,
        "open_time": ev.open_time,
        "entry_time": ev.entry_time,
        "sale_datetime": sale,
        "is_first_come": bool(ev.is_first_come),
        "ticket_url": ev.ticket_url,
        "ticket_types": [t.model_dump() for t in ev.ticket_types],
        "max_tickets": None,
        "other_details": clean_venue(ev.venue),
        "raw_text": None,
        "tweet_url": _site_event_url(ev, site_url, page_tags, anchors),
    }


# 公演ごとの詳細ページに書かれているチケットの販売ページ(本文にURLだけが書かれていることも多い)
_DETAIL_TICKET_RE = re.compile(
    r"https?://(?:t-dv\.com/[A-Za-z0-9_\-]+|ticketdive\.com/event/[A-Za-z0-9_\-]+"
    r"|tiget\.net/events/\d+|(?:t\.)?livepocket\.jp/e/[A-Za-z0-9_\-]+"
    r"|eplus\.jp/(?:sf/detail/[0-9A-Za-z\-]+|[A-Za-z0-9_\-]+/?)(?=[\s)）]|$)"
    r"|w\.pia\.jp/t/[A-Za-z0-9_\-]+/?|t\.pia\.jp/pia/event/event\.do\?eventCd=\d+"
    r"|l-tike\.com/(?:order/\?gLcode=\d+|[A-Za-z0-9_\-/]+))", re.M)
# 「開催場所」「会場」の欄(「【会場】」とも書く)。値は見出しの次の行に書かれることが多い
_DETAIL_VENUE_RE = re.compile(
    r"(?:^|\n)\s*[【［\[]?\s*(?:開催場所|会場)\s*[】］\]]?\s*[:：]?\s*\n?\s*([^\n【［\[]{2,40})")
# 「【出演者】」「出演予定」「LINE UP」の欄。次の見出し(【…】)・注記(※)までを読む
_DETAIL_LINEUP_RE = re.compile(
    r"(?:^|\n)\s*[【［\[]?\s*(?:出演者?|出演予定|LINE\s*UP|ラインナップ)[^\n】］\]]{0,6}[】］\]]?\s*[:：]?"
    r"(.{0,500}?)(?=\n\s*[【［\[※]|\Z)", re.S | re.I)
# 「10:40open」のように時刻が先に来る書き方
_OPEN_AFTER_RE = re.compile(r"(\d{1,2})[:：](\d{2})\s*(?:open|OPEN|開場)")
# 年つきで書かれた発売の開始(「受付期間：2026年10月18日(日)10:00～」)。後ろに「まで」が続くものは締切
_DETAIL_SALE_RE = re.compile(
    r"(先着|一般発売|一般販売|受付期間|販売期間|発売日時|発売開始|先行受付|抽選受付|プレイガイド)"
    r"[^\n]{0,24}\n?[^\d\n]{0,12}(20\d\d)\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})日?"
    r"[^\d\n]{0,6}(\d{1,2})[:：](\d{2})(?!\s*(?:まで|迄))")
# 「開場日時 13:45」のように開場と時刻の間に語が入る書き方
_OPEN_LOOSE_RE = re.compile(r"開場[^\d\n]{0,4}(\d{1,2})[:：](\d{2})")
_detail_cache: dict = {}


def _detail_page_text(url: str) -> Optional[str]:
    """詳細ページの本文(記事の部分だけ。サイドバーの別公演を拾わないように)。"""
    if url in _detail_cache:
        return _detail_cache[url]
    text = None
    html = sites._get(url)
    if not html or len(sites._html_to_text(html)) < 200:
        # 証明書の不備で普通には取れないサイト(私立恵比寿中学)や、JS で描くページは
        # ブラウザで開く
        html = sites._fetch_subpage(url)
    if html:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "aside"]):
            t.decompose()
        main = soup.find("article") or soup.find("main") or soup.body or soup
        text = re.sub(r"\n\s*\n+", "\n", main.get_text("\n"))
        # サイドバー(カテゴリー・アーカイブの一覧)より前だけを使う
        text = re.split(r"\n\s*(?:カテゴリー|アーカイブ|関連記事|最近の投稿)\s*\n", text)[0]
        # 販売ページへのリンクは文字が「イープラス」「チケットぴあ」だけのことが多く、
        # 本文に URL が出てこない(いぎなり東北産の生誕祭)。リンク先も本文の後ろに足す
        hrefs = [a["href"].replace("&amp;", "&") for a in main.find_all("a", href=True)]
        tickets = [h for h in hrefs if _DETAIL_TICKET_RE.search(h)]
        if tickets:
            text += "\n" + "\n".join(dict.fromkeys(tickets))
    _detail_cache[url] = text
    return text


def detail_lineup_text(row: dict, site_url: str) -> str:
    """公演の詳細ページの出演者欄(その公演日が書かれたページだけ)。無ければ空。"""
    url = row.get("tweet_url") or ""
    m = re.match(r"\d{4}-(\d{2})-(\d{2})", (row.get("event_datetime") or "")[:10])
    if (not m or urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(site_url).netloc
            or url.rstrip("/") == site_url.rstrip("/")):
        return ""
    text = _detail_page_text(url)
    if not text or (int(m.group(1)), int(m.group(2))) not in sites.dates_in_text(text):
        return ""
    lu = _DETAIL_LINEUP_RE.search(text)
    return lu.group(1).strip() if lu else ""


def enrich_site_row_from_detail(row: dict, site_url: str) -> dict:
    """公式サイトの行に、その公演の詳細ページからチケットのリンク・開場時刻・会場を補う。

    スケジュール一覧には「11/1【sun】LIVE-ATTACK!! in 榴岡野音 @仙台」のように
    日付と公演名しか無く、時刻・会場・チケットは公演ごとのページにだけ書かれている
    ことが多い(POEM の公式サイト)。チケットのリンクが入れば、その販売ページの
    出演者一覧から共演グループにも公演が入る。
    詳細ページがその公演のものか(公演日が書かれているか)を確かめてから使う。
    """
    url = row.get("tweet_url") or ""
    host = urllib.parse.urlparse(site_url).netloc
    if (not url or urllib.parse.urlparse(url).netloc != host
            or url.rstrip("/") == site_url.rstrip("/")):
        return row
    if row.get("ticket_url") and row.get("open_time") and row.get("other_details"):
        return row
    date = (row.get("event_datetime") or "")[:10]
    m = re.match(r"\d{4}-(\d{2})-(\d{2})$", date)
    text = _detail_page_text(url) if m else None
    if not text or (int(m.group(1)), int(m.group(2))) not in sites.dates_in_text(text):
        return row
    if not row.get("ticket_url"):
        t = _DETAIL_TICKET_RE.search(text)
        if t:
            row["ticket_url"] = t.group(0)
    if not row.get("open_time"):
        # 「10:40open 11:00start」は先に時刻が来る。開場の後ろの語から読むと開演(11:00)を拾う
        o = _OPEN_AFTER_RE.search(text) or None
        open_t = f"{int(o.group(1)):02d}:{o.group(2)}" if o else timetree.extract_open_entry(text)[0]
        if not open_t:
            o = _OPEN_LOOSE_RE.search(text)
            open_t = f"{int(o.group(1)):02d}:{o.group(2)}" if o else None
        row["open_time"] = open_t
    if not row.get("sale_datetime"):
        # 「一般発売(先着)受付 受付期間：2026年10月18日(日)10:00～」(締切だけのものは読まない)
        sm = _DETAIL_SALE_RE.search(text)
        if sm:
            sale = f"{sm.group(2)}-{int(sm.group(3)):02d}-{int(sm.group(4)):02d} {int(sm.group(5)):02d}:{sm.group(6)}:00"
            near = text[sm.start():sm.end() + 40]
            first_come = ("先着" in near or "一般" in near) and "抽選" not in near
        else:
            sale, first_come = timetree._regex_note_ticket(text, datetime.date.fromisoformat(date))
        if sale and sale[:10] <= date:
            row["sale_datetime"] = to_timestamptz(sale)
            row["is_first_come"] = bool(first_come)
    v = _DETAIL_VENUE_RE.search(text)
    # 一覧から取れた会場が地名だけ(「郡山」「仙台」)なら、詳細ページの会場名にする
    if v and len(row.get("other_details") or "") <= 4:
        row["other_details"] = clean_venue(v.group(1).strip()) or row.get("other_details")
    return row


def build_gcal_row(slug: str, name: str, gc_url: str, ev: dict) -> Optional[dict]:
    """Google カレンダーの整形済みイベントを auto_events 行に変換。"""
    if not ev.get("title"):
        return None
    open_t, entry_t = timetree.extract_open_entry(ev.get("note") or "")
    return {
        "tweet_id": f"gc:{slug}:{ev['id']}",
        "idol_slug": slug,
        "account": None,
        "event_name": ev["title"],
        "event_datetime": normalize_event_datetime(ev.get("event_datetime")),
        "open_time": open_t,
        "entry_time": entry_t,
        "stage_time": stage_time_of(ev),
        "sale_datetime": to_timestamptz(ev.get("sale_datetime")),
        "is_first_come": bool(ev.get("is_first_come")),
        "ticket_url": ev.get("ticket_url"),
        "ticket_types": [],
        "max_tickets": None,
        "other_details": venue_of(ev),
        "raw_text": ev.get("note") or None,
        "tweet_url": ev.get("page_url") or gc_url,
    }


def run_gcal() -> int:
    """Google 公開カレンダー(ICS)を巡回してイベントを取り込む。

    公式サイトにカレンダーを埋め込んでいるグループが一定数あり、公開設定なら
    ICS が誰でも取得できる。構造化データなので LLM は使わない。
    """
    watch = build_gcal_watch_list()
    if not watch:
        print("Googleカレンダーの監視対象がありません。")
        return 0
    print(f"Googleカレンダー監視対象: {len(watch)} 件")
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    lineup_idx = lineup.build_index(build_idol_name_list())
    total = 0
    fanout = 0
    fanout_slugs = set()
    gc_shrunk = []   # 反映より削除が多かったグループ(退行の早期発見用)
    for entry in watch:
        slug, name, gc_url = entry["idol_slug"], entry["name"], entry["gc_url"]
        cal_id = gcal.extract_calendar_id(gc_url)
        if not cal_id:
            print(f"{name}: カレンダーIDを取れません({gc_url[:60]})")
            continue
        print(f"{name} のGoogleカレンダーを確認: {cal_id[:48]}")
        try:
            content_hash, events = gcal.fetch_public_events(cal_id)
        except Exception as e:
            print(f"  失敗: {e}")
            continue
        if content_hash is None:
            continue
        state_key = f"gc:{cal_id}"
        if sb.get_last_seen(state_key) == content_hash:
            print("  変更なし。スキップ")
            continue
        rows, emitted = [], set()
        for ev in events:
            sale_dt, first_come = timetree.extract_note_ticket(
                gemini_client, ev["title"], ev.get("note") or "", today)
            if sale_dt:
                ev["sale_datetime"] = normalize_datetime_string(sale_dt) or sale_dt
            ev["is_first_come"] = first_come
            row = build_gcal_row(slug, name, gc_url, ev)
            if row:
                rows.append(row)
                emitted.add(row["tweet_id"])
        try:
            sb.upsert_auto_events(rows)
        except Exception as e:
            print(f"  書き込み失敗({len(rows)}件): {e}")
            continue
        for r in rows:
            fanout += _fanout_lineup(r, lineup_idx, today, fanout_slugs)
        removed = 0
        try:
            for old_id in sb.get_auto_event_ids(slug, prefix=f"gc:{slug}:"):
                if old_id not in emitted:
                    sb.delete_auto_event(old_id)
                    removed += 1
        except Exception as e:
            print(f"  再突合(削除)でエラー: {e}")
        sb.set_last_seen(state_key, content_hash)
        print(f"  {len(rows)} 件反映 / {removed} 件削除")
        if removed > len(rows):
            gc_shrunk.append((name, len(rows), removed))
        total += len(rows)
    if gc_shrunk:
        print("⚠ 反映より削除が多かったグループ: "
              + " / ".join(f"{n}({c}件反映・{r}件削除)" for n, c, r in gc_shrunk))
    x_removed = sum(_cleanup_past_by_date_suffix(s2, today, "ttx") for s2 in fanout_slugs)
    print(f"Googleカレンダー取り込み完了: 合計 {total} 件 / "
          f"出演者への相互反映 {fanout} 件({len(fanout_slugs)}グループ) / 過去分 {x_removed} 件削除")
    return total


# 1回のフル実行で e+ を見るグループ数。解決済みURLはキャッシュするので
# 2回目以降は1グループ1リクエストで済む。
EPLUS_BATCH = int(os.environ.get("EPLUS_BATCH") or 60)


def build_eplus_row(slug: str, name: str, page_url: str, ev: dict) -> Optional[dict]:
    """e+ の整形済みイベントを auto_events 行に変換。"""
    if not ev.get("title"):
        return None
    return {
        "tweet_id": f"ep:{slug}:{ev['id']}",
        "idol_slug": slug,
        "account": None,
        "event_name": ev["title"],
        "event_datetime": normalize_event_datetime(ev.get("event_datetime")),
        "open_time": None,
        "entry_time": None,
        "sale_datetime": None,
        "is_first_come": False,
        "ticket_url": ev.get("ticket_url"),
        "ticket_types": [],
        "max_tickets": None,
        "other_details": venue_of(ev),
        "raw_text": None,
        "tweet_url": ev.get("page_url") or page_url,
    }


def run_eplus() -> int:
    """イープラスのアーティストページから公演を取り込む。

    メジャー/大箱系はチケットを ぴあ・e+・ローソン で売るため、
    ticketdive/livepocket/tiget しか見ない Yahoo 発見では届かない。
    e+ は JSON-LD の schema.org/Event を持っているので LLM 不要で取れる。
    """
    idols = build_idol_name_list()
    if not idols:
        return 0
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    # 出演情報が少ないグループを優先して回す(多いグループは他経路で取れている)
    try:
        counts = sb.get_upcoming_counts(today)
    except Exception:
        counts = {}
    try:
        cached = sb.get_last_seen_by_prefix("epu:")
    except Exception:
        cached = {}
    # 出演情報が少ないグループを優先(多いグループは他経路で取れている)。
    # 次に、前回解決した日が古いものから。
    idols = sorted(idols, key=lambda i: (counts.get(i["idol_slug"], 0),
                                         cached.get(f"epd:{i['idol_slug']}") or ""))
    picked = idols[:EPLUS_BATCH]
    print(f"イープラス: {len(picked)} / {len(idols)} 組を確認します")
    # e+ の行は出演者欄を持たないので、対バンの相互反映は行わない。
    total = 0
    resolved = []
    for idol in picked:
        slug, name = idol["idol_slug"], idol["name"]
        page = cached.get(f"epu:{slug}")
        if page == "-":
            continue  # 前回「e+ に該当なし」と分かっているので検索しない
        if not page:
            try:
                page = eplus.resolve_page(name)
            except Exception as e:
                print(f"  {name}: e+ の解決に失敗: {e}")
                continue
            resolved.append((f"epu:{slug}", page or "-"))
            if not page:
                continue
        resolved.append((f"epd:{slug}", today))
        try:
            content_hash, events = eplus.fetch_public_events(page)
        except Exception as e:
            print(f"  {name}: e+ の取得に失敗: {e}")
            continue
        if content_hash is None:
            continue
        state_key = f"ep:{page}"
        if sb.get_last_seen(state_key) == content_hash:
            continue
        rows, emitted = [], set()
        for ev in events:
            row = build_eplus_row(slug, name, page, ev)
            if row:
                rows.append(row)
                emitted.add(row["tweet_id"])
        if rows:
            try:
                sb.upsert_auto_events(rows)
            except Exception as e:
                print(f"  {name}: 書き込み失敗({len(rows)}件): {e}")
                continue
            print(f"  {name}: {len(rows)} 件反映")
            total += len(rows)
        removed = 0
        try:
            for old_id in sb.get_auto_event_ids(slug, prefix=f"ep:{slug}:"):
                if old_id not in emitted:
                    sb.delete_auto_event(old_id)
                    removed += 1
        except Exception as e:
            print(f"  {name}: 再突合(削除)でエラー: {e}")
        sb.set_last_seen(state_key, content_hash)
    try:
        sb.set_last_seen_many(resolved)
    except Exception as e:
        print(f"  e+ の解決結果の記録に失敗: {e}")
    print(f"イープラス取り込み完了: 合計 {total} 件")
    return total


def _site_event_dedup_key(name, datetime_str):
    """同一公演の重複判定キー(正規化した名前 + 日付部分)。"""
    n = re.sub(r"\s+", "", (name or "").lower())
    d = (normalize_event_datetime(datetime_str) or "")[:10]
    return (n, d)


def merge_site_events(jsonld_events, gemini_events):
    """JSON-LD(構造化データ・高精度)を優先し、Geminiが拾った未カバーの公演を追加する。

    構造化データに同じ名前の公演があるのに、LLM がそれと違う日付で出したものは捨てる。
    LLM は公演名と日付の対応を取り違えることがある(ばってん少女隊の 11/3
    「MATSURI-10」in 東京が、11/1 の行としても入っていた)。同じ名前で本当に
    複数日ある公演は、構造化データにも日ごとに並ぶ。
    """
    ld_days = {}
    for ev in jsonld_events:
        if getattr(ev, "event_name", None):
            n, d = _site_event_dedup_key(ev.event_name, ev.event_datetime)
            ld_days.setdefault(n, set()).add(d)
    merged = []
    seen = set()
    for ev in list(jsonld_events) + [
        e for e in gemini_events
        if not (getattr(e, "event_name", None)
                and (k := _site_event_dedup_key(e.event_name, e.event_datetime))[0] in ld_days
                and k[1] not in ld_days[k[0]])
    ]:
        if not getattr(ev, "event_name", None):
            continue
        k = _site_event_dedup_key(ev.event_name, ev.event_datetime)
        if k in seen:
            continue
        seen.add(k)
        merged.append(ev)
    return merged


# 1回のフル実行で巡回する公式サイト数の上限。1サイトあたり約36秒(描画とサブページ
# 取得を含む)なので、登録サイトが増えても実行時間が一定になるようにする。
# 1回に巡回する公式サイトの数。実測で1組17〜26秒(JS描画+サブページ取得)かかり、
# 65組だとフル巡回42分の半分以上をここで使っていた。一方 LLM の日次枠で実際に
# 抽出できるのは十数組で、残りは取得時間を払うだけになっていた。枠に見合う数に絞る。
SITE_BATCH = int(os.environ.get("SITE_BATCH") or 30)
# そのうち、前回の抽出に失敗したサイトの読み直しに使う上限
SITE_RETRY_MAX = int(os.environ.get("SITE_RETRY_MAX") or 10)
# LLMが連続でこの回数使えなければ、その回の公式サイト巡回は打ち切る
LLM_DEAD_LIMIT = int(os.environ.get("LLM_DEAD_LIMIT") or 8)
# TimeTree も同様。1カレンダーはAPI1回だが、各イベントの TicketDive 補完に
# 通信が入るため件数に比例して伸びる。
TT_BATCH = int(os.environ.get("TT_BATCH") or 45)


# 公演がこの日数内のグループを「直前」として先に巡回する。出演時刻は公演の
# 直前に書き込まれるため、ここを過ぎてから読んでも間に合わない。
IMMINENT_DAYS = int(os.environ.get("IMMINENT_DAYS") or 4)
# 1回の巡回枠のうち、直前のグループに割り当てる割合。全部を直前優先にすると
# 出演予定が無いグループ(=取り込めていないグループ)がいつまでも読まれない。
IMMINENT_SHARE = float(os.environ.get("IMMINENT_SHARE", "0.6"))


def _rotate(watch, today: str, key_prefix: str, batch: int, label: str, record: bool = True):
    """巡回対象を上限 batch 件に絞る。登録数が増えても実行時間を一定に保つ。

    枠を2つに分ける。
      ① 公演が近い順(IMMINENT_DAYS 日内)。枠の IMMINENT_SHARE を割り当てる
      ② 今後のイベントが少ない順 → 最後に巡回した日が古い順。残りの枠

    ①が要るのは出演時刻のため。「出演19:35-20:00」は公演の1〜3日前に各グループが
    カレンダーへ書き込むので、直前のグループを後回しにするとタイムテーブルを
    埋められないまま当日を過ぎてしまう。
    ②を残すのは、出演予定が1件も取れていないグループを飢えさせないため。
    内容ハッシュで抽出はスキップできるが、ハッシュを出すための取得自体は毎回
    走るので、ここで絞らないと登録数に比例して時間が伸びる。
    """
    if len(watch) <= batch:
        return watch
    try:
        counts = sb.get_upcoming_counts(today)
    except Exception as e:
        print(f"  カバー状況の取得に失敗(登録順に巡回します): {e}")
        return watch[:batch]
    try:
        last_by_key = sb.get_last_seen_by_prefix(f"{key_prefix}:")
    except Exception:
        last_by_key = {}
    try:
        next_date = sb.get_next_event_dates(today)
    except Exception as e:
        print(f"  次の出演予定の取得に失敗(この優先は使いません): {e}")
        next_date = {}
    limit = (datetime.date.fromisoformat(today)
             + datetime.timedelta(days=IMMINENT_DAYS)).isoformat()

    def last_seen(e):
        return last_by_key.get(f"{key_prefix}:{e['idol_slug']}") or ""

    soon = sorted(
        (e for e in watch if today <= (next_date.get(e["idol_slug"]) or "9999") < limit),
        key=lambda e: (next_date[e["idol_slug"]], last_seen(e)),
    )
    rest = sorted(
        (e for e in watch if e not in soon),
        key=lambda e: (counts.get(e["idol_slug"], 0), last_seen(e)),
    )
    n_soon = min(len(soon), int(batch * IMMINENT_SHARE))
    picked = soon[:n_soon] + rest[: batch - n_soon]
    # 片方が足りなければもう片方で埋める
    if len(picked) < batch:
        extra = [e for e in soon[n_soon:] + rest if e not in picked]
        picked += extra[: batch - len(picked)]
    # record=False のときは呼び出し側が「実際に処理できたグループ」だけを記録する
    # (枠切れで抽出できなかったグループまで巡回済みにすると、次の順番が一巡後に
    # なってしまい、いつまでも取り込めない)
    if record:
        try:
            sb.set_last_seen_many([(f"{key_prefix}:{e['idol_slug']}", today) for e in picked])
        except Exception as e2:
            print(f"  巡回日の記録に失敗(次回も同じ順番になります): {e2}")
    thin = sum(1 for e in picked if counts.get(e["idol_slug"], 0) < 3)
    print(f"  今回の{label}巡回は {len(picked)} / {len(watch)} 件"
          f"({IMMINENT_DAYS}日内に公演 {n_soon} 件・今後3件未満 {thin} 件を優先・残りは次回)")
    return picked


def _note_site_calendars(slug: str, name: str, cal_links) -> int:
    """公式サイトに貼られていた公開カレンダーを、巡回対象として記録する。

    予定表をJSで描くサイトからLLMで読み取るより、カレンダーそのものを読むほうが
    確実で、無料枠も使わない。別グループのカレンダーを貼っているサイトもあるので、
    TimeTree はカレンダー名がグループ名と一致するものだけ採る。
    Google カレンダーは名前が引けないので、そのグループのサイトに貼られている
    ことを根拠に採る(サイト自体はグループ名の確認を通っている)。
    """
    if not cal_links:
        return 0
    pairs = []
    keys = set()
    checked = set()
    for u in cal_links[:12]:
        m = re.search(r"(?:public_calendars/|timetr\.ee/p(?:c)?/)([A-Za-z0-9_.\-]+)", u)
        if m:
            alias = m.group(1)
            if alias in checked or len(checked) >= 3:
                continue   # 同じカレンダーのイベントページが並ぶことがある
            checked.add(alias)
            title = timetree.calendar_name(alias)
            if title and timetree.calendar_belongs_to(name, title):
                if f"ttfound:{slug}" in keys:
                    continue   # 1グループにつき1件
                print(f"    ★ 公式サイトのTimeTree「{title}」を巡回対象にします({alias})")
                keys.add(f"ttfound:{slug}")
                pairs.append((f"ttfound:{slug}", alias))
            elif title:
                print(f"    公式サイトのTimeTree「{title}」は別物として見送り({alias})")
            continue
        cal_id = gcal.extract_calendar_id(u)
        # 同じカレンダーが複数の書き方で貼られていることがあるので、1組につき1件。
        if cal_id and "@" in cal_id and f"gcfound:{slug}" not in keys:
            print(f"    ★ 公式サイトのGoogleカレンダーを巡回対象にします({cal_id})")
            keys.add(f"gcfound:{slug}")
            pairs.append((f"gcfound:{slug}", cal_id))
    if not pairs:
        return 0
    try:
        sb.set_last_seen_many(pairs)
    except Exception as e:
        print(f"    カレンダーの記録に失敗: {e}")
        return 0
    return len(pairs)


def _site_mentions_group(name: str, text: str) -> bool:
    """サイト本文にグループ名が出てくるか。

    図鑑に登録した公式サイトURLが、そのグループと無関係なサイトになっていること
    がある(ドメインが手放されて別の用途で使われている等。MiX BeRRY の mixberry.jp は
    中国ドラマのブログになっていた)。名前がどこにも出てこないサイトから予定を
    取り出そうとしても無駄なので、先に弾く。
    """
    light = lineup._light(name)
    # 図鑑の名前に付けている補足(「PassCode(Band Set)」の括弧など)はサイトには
    # 書かれていないので、括弧を外した名前でも見る。
    base = lineup._light(re.sub(r"[（(\[［].{0,20}?[）)\]］]", "", name)).strip()
    if len(light) < lineup.MIN_NAME_LEN:
        return True   # 短い名前は偶然の一致・不一致が多いので判定しない
    hay = lineup._light(text)
    if light in hay or (len(base) >= lineup.MIN_NAME_LEN and base in hay):
        return True
    light = base if len(base) >= lineup.MIN_NAME_LEN else light
    # 「MiX BeRRY」↔「mixberry」のように、空白や記号の有無だけが違う表記も同じと見る
    squash = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]", "", light)
    return len(squash) >= lineup.MIN_NAME_LEN and squash in re.sub(
        r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]", "", hay)


# 1回の実行で公演ページを読み直す数と、同じ行を読み直すまでの日数
DETAIL_REFRESH_LIMIT = int(os.environ.get("DETAIL_REFRESH_LIMIT") or 60)
DETAIL_RECHECK_DAYS = int(os.environ.get("DETAIL_RECHECK_DAYS") or 3)
_DETAIL_FIELDS = ("ticket_url", "open_time", "other_details", "sale_datetime", "is_first_come",
                  "ticket_types", "event_name", "event_datetime")


# 1回の実行で、トップに案内している行のために読み直すサイトの数
DETAIL_RELINK_SITES = int(os.environ.get("DETAIL_RELINK_SITES") or 20)


def _relink_site_rows(rows, site_of, only_slugs, seen, before, is_detail) -> set:
    """トップ・一覧のページに案内している公式サイトの行に、公演ページを付け直す。

    公演ページは LLM で抜き出したときにしか探していなかったので、そのあと探し方を
    よくしても、サイトの内容が変わるまで古い案内先のままだった。サイトを読んで
    (LLM なし)予定表・カレンダーの項目のリンクと特設ページの見出しから探し直す。
    戻り値: 付け直した行の tweet_id
    """
    def needs(r) -> bool:
        url = r.get("tweet_url") or ""
        return (not is_detail(r)) or bool(_LIST_PAGE.search(urllib.parse.urlparse(url).path))

    by_slug = {}
    for r in rows:
        if r["idol_slug"] in site_of and (not only_slugs or r["idol_slug"] in only_slugs) and needs(r):
            by_slug.setdefault(r["idol_slug"], []).append(r)
    slugs = [s for s in by_slug if only_slugs or (seen.get(f"relink:{s}") or "") < before]
    if not only_slugs:
        slugs = slugs[:DETAIL_RELINK_SITES]
    done, changed = [], set()
    for slug in slugs:
        site = site_of[slug]
        try:
            _h, _c, _ld, _cal, page_tags, anchors = sites.fetch_site_content(site)
        except Exception as e:
            print(f"  {slug}: サイトの読み直しに失敗: {e}")
            continue
        done.append(slug)
        for r in by_slug[slug]:
            ev = sites.SiteEvent(event_name=r["event_name"] or "", event_datetime=r["event_datetime"])
            url = _site_event_url(ev, site, page_tags, anchors)
            if url and url != r.get("tweet_url") and url.rstrip("/") != site.rstrip("/") \
                    and not _LIST_PAGE.search(urllib.parse.urlparse(url).path):
                try:
                    sb.patch_auto_event(r["tweet_id"], {"tweet_url": url})
                    r["tweet_url"] = url
                    changed.add(r["tweet_id"])
                except Exception as e:
                    print(f"  {r['event_name'][:30]}: 案内先の書き込み失敗: {e}")
    if done:
        try:
            sb.set_last_seen_many([(f"relink:{s}", datetime.datetime.now(JST).strftime("%Y-%m-%d"))
                                   for s in done])
        except Exception:
            pass
        print(f"公演ページの付け直し: {len(done)} サイトを読み、{len(changed)} 件の案内先を公演ページにした")
    return changed


def refresh_site_details(only_slugs=None) -> int:
    """取り込み済みの公式サイトの公演に、公演ページの中身(販売ページ・開場・会場・発売・出演者)を足す。

    公演ページは LLM で公演を抜き出すときにしか読んでいなかったため、サイトの内容が
    変わらない・LLM の枠が尽きている間は、販売ページの分かる公演でもアプリから
    公式サイトのトップにしか案内できなかった(いぎなり東北産の安杜羽加生誕祭)。
    LLM は使わない。公演の近いものから、DETAIL_RECHECK_DAYS 日おきに読み直す。
    only_slugs を渡すと、そのグループの行を日数に関係なく全部読む(手動実行用)。
    """
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        site_of = {w["idol_slug"]: w["site_url"] for w in build_site_watch_list(include_skipped=True)}
        rows = sb.get_site_rows_for_detail(today)
        seen = {} if only_slugs else {**sb.get_last_seen_by_prefix("detail:"),
                                      **sb.get_last_seen_by_prefix("relink:")}
    except Exception as e:
        print(f"公演ページの読み直し: 準備に失敗(見送ります): {e}")
        return 0
    before = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=DETAIL_RECHECK_DAYS)).isoformat()

    def is_detail(r) -> bool:
        site = site_of.get(r["idol_slug"]) or ""
        url = r.get("tweet_url") or ""
        return (bool(site) and urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(site).netloc
                and url.rstrip("/") != site.rstrip("/"))

    # トップや一覧のページに案内している行は、サイトを読み直して(LLM なし)公演ページを付け直す
    relinked = _relink_site_rows(rows, site_of, only_slugs, seen, before, is_detail)
    cand = [r for r in rows
            if (not only_slugs or r["idol_slug"] in only_slugs) and is_detail(r)
            and ((seen.get(f"detail:{r['tweet_id']}") or "") < before or r["tweet_id"] in relinked)]
    if not only_slugs:
        cand = cand[:DETAIL_REFRESH_LIMIT]
    if not cand:
        return 0
    try:
        lineup_idx = lineup.build_index(build_idol_name_list())
    except Exception:
        lineup_idx = []
    updated = fanout = 0
    touched = set()
    for r in cand:
        site = site_of[r["idol_slug"]]
        old = dict(r)
        try:
            enrich_site_row_from_detail(r, site)
            enrich_row_with_ticketdive(r)
        except Exception as e:
            print(f"  {r['event_name'][:30]}: 公演ページの読み直しに失敗: {e}")
            continue
        patch = {k: r.get(k) for k in _DETAIL_FIELDS if r.get(k) != old.get(k)}
        if patch:
            try:
                sb.patch_auto_event(r["tweet_id"], patch)
                updated += 1
            except Exception as e:
                print(f"  {r['event_name'][:30]}: 書き込み失敗: {e}")
        if lineup_idx:
            names = detail_lineup_text(r, site)
            if names:
                fanout += _fanout_lineup(dict(r, raw_text=names), lineup_idx, today, touched)
    try:
        sb.set_last_seen_many([(f"detail:{r['tweet_id']}", today) for r in cand])
    except Exception as e:
        print(f"  公演ページを読んだ日の記録に失敗: {e}")
    print(f"公演ページの読み直し: {len(cand)} 件を読み、{updated} 件に販売ページ・開場などを補った"
          + (f" / 出演者から {fanout} 件をほかのグループに反映" if fanout else ""))
    return updated


def run_sites() -> int:
    """公式サイトを巡回してイベント/チケット情報を取り込む(主軸)。
    JSON-LD(schema.org Event)を Gemini不要で抽出し、Gemini解析結果と統合する。"""
    # SITE_SLUGS を渡すと、そのグループだけを順番待ち(ローテーション)や
    # site_skip.json に関係なく巡回する。取れないサイトの原因を短時間で確かめる用。
    only = [s for s in os.environ.get("SITE_SLUGS", "").split(",") if s.strip()]
    # 取り込み済みの公演の公演ページを先に読み直す(LLM を使わないので、枠が尽きていても進む)
    refresh_site_details(set(only) if only else None)
    watch = build_site_watch_list(include_skipped=bool(only))
    if not watch:
        print("公式サイトの監視対象がありません(図鑑に公式サイトURLが未登録の可能性)。")
        return 0
    print(f"公式サイト監視対象: {len(watch)} 件")
    if only:
        want = {s.strip() for s in only}
        watch = [w for w in watch if w["idol_slug"] in want]
        print(f"SITE_SLUGS 指定: {len(watch)} 件のみ巡回します")
        missing = want - {w["idol_slug"] for w in watch}
        if missing:
            print(f"  公式サイトリンクが無く巡回できない slug: {', '.join(sorted(missing))}")
    else:
        # 前回の抽出で失敗の印(本文に今後の日付があるのに0件 = |z1、大きく減った = |low)が
        # 付いたサイトは、順番を待たずに今回読み直す。順番は「今後の公演が少ない順」なので、
        # ほかの経路で何件か取れているグループ(POEM)は公式サイトが0件のまま
        # 何日も回ってこず、目の前の公演を取りこぼしていた。
        try:
            states = sb.get_last_seen_by_prefix("site:")
        except Exception as e:
            print(f"  抽出の状態の取得に失敗(読み直しの優先はしません): {e}")
            states = {}
        retry = [w for w in watch
                 if str(states.get(f"site:{w['idol_slug']}") or "").endswith(("|z1", "|low"))]
        retry = retry[:SITE_RETRY_MAX]
        if retry:
            print(f"  前回抽出に失敗したサイトを先に読み直します: {len(retry)} 件")
        rest = [w for w in watch if w not in retry]
        watch = retry + _rotate(rest, datetime.datetime.now(JST).strftime("%Y-%m-%d"),
                                "sq", max(SITE_BATCH - len(retry), 0), "公式サイト", record=False)
    total = 0
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    processed = []   # 巡回日を記録するグループ(枠切れで抽出できなかった分は入れない)
    found_cal = 0    # 公式サイトから見つけた公開カレンダーの数
    shrunk = []      # 反映より削除が多かったグループ(退行の早期発見用)
    try:
        # すでにカレンダーがあるグループは探さない(カレンダー名の確認で毎回
        # 取得が入るため)。自動発見済みの分もこの一覧に入っている。
        cal_known = ({e["idol_slug"] for e in build_timetree_watch_list()}
                     | {e["idol_slug"] for e in build_gcal_watch_list()})
    except Exception as e:
        print(f"  既存カレンダーの確認に失敗(全グループで探します): {e}")
        cal_known = set()
    dead = 0         # LLMが連続して使えなかった回数
    # 公演名や詳細ページの出演者欄に名前がある、ほかの登録グループにも公演を入れる
    # (「#スタダ顔 ライブ」は私立恵比寿中学のサイトにしか無く、出演する
    #  ばってん少女隊・いぎなり東北産のページに出ていなかった)
    try:
        lineup_idx = lineup.build_index(build_idol_name_list())
    except Exception as e:
        print(f"  出演者の相互反映に使う一覧の取得に失敗(反映しません): {e}")
        lineup_idx = []
    fanout = 0
    fanout_slugs = set()
    for entry in watch:
        slug, name, url = entry["idol_slug"], entry["name"], entry["site_url"]
        print(f"{name} の公式サイトを確認: {url}")
        try:
            (content_hash, combined, jsonld_events,
             cal_links, page_tags, anchors) = sites.fetch_site_content(url)
        except Exception as e:
            print(f"  失敗: {e}")
            processed.append(slug)
            continue
        if content_hash is None:
            processed.append(slug)
            continue
        if not _site_mentions_group(name, combined):
            # 登録URLがそのグループのサイトでない(ドメインが別の用途に使われている等)。
            # LLMの無料枠を使わないよう抽出しない。状態は書かないので、URLを直せば
            # 次の巡回でそのまま取り込まれ、直っていなければ毎回ログに出て気づける。
            print(f"  サイト内にグループ名が出てきません。登録URLの見直しが必要です: {url}")
            processed.append(slug)
            continue
        if slug not in cal_known:
            found_cal += _note_site_calendars(slug, name, cal_links)
        state_key = f"site:{slug}"
        # 本文/構造化データに変化が無ければ LLM を呼ばずにスキップ(無料枠の節約)。
        # ただし「本文に今後の日付があるのに抽出0件」だった回は <hash>|z1 を記録してあり、
        # 内容が同じでも一度だけ取り直す(取りこぼしがそのまま固定化するのを防ぐ)。
        seen = sb.get_last_seen(state_key)
        # グループを指定した手動実行(SITE_SLUGS)は、取り込みを直したあとに確かめる用なので
        # 内容が同じでも読み直す
        if seen == content_hash and not only:
            print("  変更なし。スキップ")
            processed.append(slug)
            continue
        # 再取得の印は内容ハッシュに紐づけない。ニュース欄の更新などで本文が数文字
        # 変わるだけで「前回見送った」と分からなくなり、見送りを繰り返したまま
        # 新しい行だけが増え続けてしまう(超ときめき♡宣伝部で20件→30件になった)。
        retry_zero = bool(seen) and seen.endswith("|z1")
        retry_low = bool(seen) and seen.endswith("|low")
        if retry_zero:
            print("  前回0件だったため再抽出します")
        if retry_low:
            print("  前回大きく減ったため再抽出します(今回は結果をそのまま反映します)")
        llm_events = sites.extract_events_any(groq_client, gemini_client, name, combined)
        llm_ok = llm_events is not None
        if not llm_ok:
            llm_events = []
        if not llm_ok and not jsonld_events:
            # LLMが一時失敗(枠切れ等)、かつJSON-LDも無し → 状態を更新せず次回再試行
            print("  抽出できず(次回再試行)")
            dead += 1
            if dead >= LLM_DEAD_LIMIT:
                # その日の無料枠が尽きた後は、取得(JS描画を含む)に数分かけても
                # 何も取り込めない。残りは次回に回して実行時間を節約する。
                rest = len(watch) - len(processed) - dead
                print(f"  LLMの無料枠が尽きたため、残り {max(rest, 0)} 件は次回に回します")
                break
            continue
        dead = 0

        merged = merge_site_events(jsonld_events, llm_events)
        count = 0
        emitted_ids = set()
        for ev in merged:
            row = build_site_row(slug, name, url, ev, page_tags, anchors)
            if row:
                enrich_site_row_from_detail(row, url)
                enrich_row_with_ticketdive(row)
                try:
                    sb.upsert_auto_event(row)
                    emitted_ids.add(row["tweet_id"])
                    count += 1
                except Exception as e:
                    print(f"  書き込み失敗: {e}")
                    continue
                if lineup_idx:
                    names = detail_lineup_text(row, url)
                    fanout += _fanout_lineup(dict(row, raw_text=names or None), lineup_idx,
                                             today, fanout_slugs)

        if llm_ok:
            try:
                old_ids = sb.get_auto_event_ids(slug, prefix=f"site:{slug}:")
            except Exception as e:
                print(f"  既存行の取得でエラー(削除は見送り): {e}")
                old_ids = None
            # 抽出が前回の半分未満に減ったのに素材には今後の日付がある、というのは
            # 公演が消えたのではなく抽出が失敗した形(出力の打ち切り・モデルの不調)の
            # ことが多い。いぎなり東北産が13件→2件になり11件消えた事故がこれだった。
            # そういう回は削除せず、取れた分だけ足して次回やり直す。
            suspicious = (
                old_ids is not None
                and len(old_ids) >= 4
                and count < len(old_ids) / 2
                and not retry_low
                and sites.has_future_dates(combined)
            )
            removed = 0
            if suspicious:
                sb.set_last_seen(state_key, f"{content_hash}|low")
                print(f"  {count} 件しか取れず前回の {len(old_ids)} 件から大きく減るため、"
                      f"削除は見送って次回やり直します")
            else:
                try:
                    for old_id in (old_ids or []):
                        if old_id not in emitted_ids:
                            sb.delete_auto_event(old_id)
                            removed += 1
                            # その公演をほかのグループに反映した行(ttx:<先>:<slug>:<key>:<日付>)も消す
                            src = old_id[len("site:"):]
                            ttx = sb.get_auto_event_ids_like(f"ttx:*:{src}:*")
                            if ttx:
                                sb.delete_auto_events(ttx)
                except Exception as e:
                    print(f"  再突合(削除)でエラー: {e}")
                if count == 0 and not retry_zero and sites.has_future_dates(combined):
                    # 素材に今後の日付があるのに1件も取れていない → 次回もう一度だけ試す
                    sb.set_last_seen(state_key, f"{content_hash}|z1")
                    print("  0 件(本文に今後の日付あり)。次回もう一度抽出します")
                else:
                    sb.set_last_seen(state_key, content_hash)
                    print(f"  {count} 件反映 / {removed} 件削除")
                    if removed > count:
                        shrunk.append((name, count, removed))
        else:
            # LLM一時失敗: JSON-LD分だけ即反映。削除・状態更新はせず次回再試行する
            # (既存のLLM由来イベントを枠切れで消さないため、reconcileとhash更新は行わない)
            print(f"  {count} 件反映(JSON-LDのみ / LLM次回再試行)")
            dead += 1
        total += count
        if llm_ok:
            processed.append(slug)
    if fanout:
        print(f"公式サイトの公演を、出演するほかのグループにも反映: {fanout} 件 / {len(fanout_slugs)} グループ")
    if processed and not os.environ.get("SITE_SLUGS", "").strip():
        try:
            sb.set_last_seen_many([(f"sq:{s2}", today) for s2 in processed])
        except Exception as e:
            print(f"  巡回日の記録に失敗(次回も同じ順番になります): {e}")
    print(f"公式サイト取り込み完了: 合計 {total} 件 / 巡回できた {len(processed)} 件"
          + (f" / 公開カレンダーを {found_cal} 件発見" if found_cal else ""))
    if shrunk:
        # 抽出の退行は「反映より削除が多い」形で現れる(いぎなり東北産が13→2件に
        # なった事故がこれ)。見落とさないよう、最後にまとめて出す。
        print("⚠ 反映より削除が多かったグループ: "
              + " / ".join(f"{n}({c}件反映・{r}件削除)" for n, c, r in shrunk))
    return total


def build_timetree_row(slug: str, name: str, tt_url: str, ev: dict) -> Optional[dict]:
    """TimeTree の整形済みイベントを auto_events 行に変換。tweet_id は安定IDで重複排除。"""
    if not ev.get("title"):
        return None
    sale = to_timestamptz(ev.get("sale_datetime"))
    open_t, entry_t = timetree.extract_open_entry(ev.get("note") or "")
    return {
        "tweet_id": f"tt:{slug}:{ev['id']}",
        "idol_slug": slug,
        "account": None,
        "event_name": ev["title"],
        "event_datetime": normalize_event_datetime(ev.get("event_datetime")),
        "open_time": open_t,
        "entry_time": entry_t,
        "stage_time": stage_time_of(ev),
        "sale_datetime": sale,
        "is_first_come": bool(ev.get("is_first_come")),
        "ticket_url": ev.get("ticket_url"),
        "ticket_types": [],
        "max_tickets": None,
        "other_details": venue_of(ev),
        "raw_text": ev.get("note") or None,
        "tweet_url": ev.get("page_url") or tt_url,
    }


# 1イベントあたりの相互反映の上限。大型フェスでも出演組数はこの範囲に収まる。
MAX_LINEUP_FANOUT = 60


def _fanout_lineup(row: dict, lineup_idx, today: str, touched: set) -> int:
    """対バン/フェスの行を、本文に名前がある他の登録グループにも反映する。

    対バンはどこか1組のカレンダーにしか載らないことが多く、そのままでは
    同じイベントに出る他のグループのページに何も出ない(ホームの「今日の現場」
    には出るのに、出演者のページには出ないという食い違いになっていた)。

    ID は ttx: で始める。tt: とは別系統にしておくことで、各グループ自身の
    カレンダーの再突合削除(出力されなかった tt: 行を消す)に巻き込まれない。
    """
    date = (row.get("event_datetime") or "")[:10]
    tid = row.get("tweet_id", "")
    # tt(TimeTree)・gc(Googleカレンダー)・site(公式サイト)の行を相互反映の対象にする。
    # 公式サイト由来の行は出演者欄を持たないが、公演名そのものに他グループの名前が
    # 入ることがある(「POEM・poemちゃん健康促進無料配信ライブ」など)。
    # ttx 自体をさらに展開しないよう、接頭辞を限定する。
    prefix = next((p for p in ("tt:", "gc:", "site:") if tid.startswith(p)), None)
    if not date or date < today or not prefix:
        return 0
    text = f"{row.get('event_name') or ''}\n{row.get('raw_text') or ''}"
    batch = []
    for disp, other in lineup.names_in_text(text, lineup_idx):
        if other == row["idol_slug"]:
            continue
        if len(batch) >= MAX_LINEUP_FANOUT:
            print(f"    出演者の相互反映が{MAX_LINEUP_FANOUT}件を超えたため打ち切りました")
            break
        xr = dict(row)
        xr["idol_slug"] = other
        xr["tweet_id"] = f"ttx:{other}:{tid[len(prefix):]}:{date.replace('-', '')}"
        # 出演時刻は元のカレンダーの持ち主のものなので、反映先には持ち込まない
        xr.pop("stage_time", None)
        batch.append(xr)
        touched.add(other)
    try:
        sb.upsert_auto_events(batch)
    except Exception as e:
        print(f"    相互反映の書き込み失敗({len(batch)}件): {e}")
        return 0
    return len(batch)


def run_timetree() -> int:
    """TimeTree 公開カレンダーを巡回してイベント/チケット情報を取り込む。"""
    watch = build_timetree_watch_list()
    if not watch:
        print("TimeTree の監視対象がありません(図鑑にTimeTreeリンクが未登録)。")
        return 0
    print(f"TimeTree 監視対象: {len(watch)} 件")
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    watch = _rotate(watch, today, "tq", TT_BATCH, "TimeTree")
    lineup_idx = lineup.build_index(build_idol_name_list())
    print(f"出演者の相互反映に使う登録グループ: {len(lineup_idx)} 件")
    total = 0
    fanout = 0
    fanout_slugs = set()
    tt_shrunk = []   # 反映より削除が多かったグループ(退行の早期発見用)
    for entry in watch:
        slug, name, tt_url = entry["idol_slug"], entry["name"], entry["tt_url"]
        alias = timetree.extract_alias(tt_url)
        if not alias:
            print(f"{name}: TimeTree alias を解決できません({tt_url})")
            continue
        print(f"{name} の TimeTree を確認: {alias}")
        try:
            content_hash, events = timetree.fetch_public_events(alias)
        except Exception as e:
            print(f"  失敗: {e}")
            continue
        if content_hash is None:
            continue
        state_key = f"tt:{alias}"
        if sb.get_last_seen(state_key) == content_hash:
            print("  変更なし。スキップ")
            continue
        count = 0
        removed = 0
        emitted_ids = set()
        for ev in events:
            # メモにチケット発売情報があれば発売日時/先着を補完(正規表現優先・取れない時だけGemini)
            sale_dt, first_come = timetree.extract_note_ticket(
                gemini_client, ev["title"], ev.get("note") or "", today)
            if sale_dt:
                ev["sale_datetime"] = normalize_datetime_string(sale_dt) or sale_dt
            ev["is_first_come"] = first_come
            row = build_timetree_row(slug, name, tt_url, ev)
            if row:
                # 複数日フェスは出演日ごとに分割してから、各行をTicketDiveで高精度化
                for r in expand_timetree_row(row, name):
                    enrich_row_with_ticketdive(r)
                    try:
                        sb.upsert_auto_event(r)
                        emitted_ids.add(r["tweet_id"])
                        count += 1
                    except Exception as e:
                        print(f"  書き込み失敗: {e}")
                        continue
                    fanout += _fanout_lineup(r, lineup_idx, today, fanout_slugs)
        # 再突合: 今回出力しなかった過去のtt行(過去イベント・削除済み・プレースホルダ等)を掃除
        try:
            for old_id in sb.get_auto_event_ids(slug, prefix=f"tt:{slug}:"):
                if old_id not in emitted_ids:
                    sb.delete_auto_event(old_id)
                    removed += 1
        except Exception as e:
            print(f"  再突合(削除)でエラー: {e}")
        sb.set_last_seen(state_key, content_hash)
        print(f"  {count} 件反映 / {removed} 件削除")
        if removed > count:
            tt_shrunk.append((name, count, removed))
        total += count
    if tt_shrunk:
        print("⚠ 反映より削除が多かったグループ: "
              + " / ".join(f"{n}({c}件反映・{r}件削除)" for n, c, r in tt_shrunk))
    x_removed = sum(_cleanup_past_by_date_suffix(s2, today, "ttx") for s2 in fanout_slugs)
    print(f"TimeTree 取り込み完了: 合計 {total} 件 / "
          f"出演者への相互反映 {fanout} 件({len(fanout_slugs)}グループ) / 過去分 {x_removed} 件削除")
    return total


async def run_x() -> int:
    """X(twikit)からの取り込み(補助・ENABLE_X_INGEST=1のときのみ)。"""
    global x_client
    from twikit import Client
    x_client = Client("en-US")
    if not await login_x():
        print("X ログイン不可。X取り込みをスキップ。")
        return 0
    accounts = build_watch_list()
    if not accounts:
        print("X監視対象がありません。")
        return 0
    print(f"X監視対象: {len(accounts)} アカウント")
    total = 0
    for entry in accounts:
        try:
            total += await check_account(entry["handle"], entry["idol_slug"])
        except Exception as e:
            print(f"{entry.get('handle')} の処理でエラー: {e}")
            traceback.print_exc()
    print(f"X取り込み完了: {total} 件。")
    return total


# Yahoo! リアルタイム検索でチケットサイトのリンクを拾うためのクエリ。
# TicketDive は検索API・一覧・sitemap を持たないため、X 上の告知投稿からURLを
# 見つけるのが唯一の能動発見手段。LivePocket / TiGET も同じ経路で拾う。
# 行儀よく少数クエリに留める。
YRT_QUERIES = [
    # アイドル文脈のクエリが最も歩留まりが良い(対バン1本で登録グループが十数件一致する)
    "tiget アイドル", "livepocket アイドル", "ticketdive アイドル",
    "アイドル 対バン チケット", "アイドル 定期公演 チケット",
    # サイト横断の取りこぼし拾い
    "ticketdive 出演", "tiget 出演", "livepocket 出演",
]


# 1回の発見実行で名前検索するグループ数。1クエリ3秒なので約2.3分。
# 発見は1日5回なので、378グループを約1.7日で一周する。
# GitHub Actions 無料枠(2,000分/月)の残りは実測で約350分/月なので、
# 5回×2.3分×30日 ≒ 345分 で収まる。
GROUP_QUERY_BATCH = int(os.environ.get("GROUP_QUERY_BATCH") or 45)


def _group_search_queries(today: str) -> List[dict]:
    """このラウンドで名前検索するグループのクエリを返す。

    優先順位は
      ① 今後のイベントが1件も登録されていないグループ
      ② 最後に検索した日が古いグループ(未検索を最優先)
    とする。カバーできていないグループへ自動的に枠が寄り、
    既に充実しているグループは後回しになる。
    """
    idols = build_idol_name_list()
    if not idols:
        return []
    try:
        counts = sb.get_upcoming_counts(today)
    except Exception as e:
        print(f"  カバー状況の取得に失敗(全グループを同等に扱います): {e}")
        counts = {}
    # 公式サイト/TimeTree が登録されているグループは別経路でも取れる。
    # 390組のうち290組はリンクが1つも無く(chuLa・AKB48 など)、この検索が
    # 唯一の取得経路になるので、そちらを先に回す。
    try:
        other = {e["idol_slug"] for e in build_site_watch_list()}
        other |= {e["idol_slug"] for e in build_timetree_watch_list()}
    except Exception as e:
        print(f"  他経路の判定に失敗(全グループを同等に扱います): {e}")
        other = set()

    try:
        last_by_key = sb.get_last_seen_by_prefix("yq:")
    except Exception as e:
        print(f"  前回検索日の取得に失敗(未検索として扱います): {e}")
        last_by_key = {}

    ranked = []
    for idol in idols:
        q = yahoo_rt.search_query_for_name(idol["name"])
        if not q:
            continue
        slug = idol["idol_slug"]
        last = last_by_key.get(f"yq:{slug}") or ""
        # ① 他経路が無いグループ ② 今後のイベントが少ないグループ
        # ③ 最後に検索した日が古いグループ の順。②を0件か否かの二択に
        # すると、対バン経由で数件だけ拾えている実態より薄いグループ
        # (chuLa は5件)が後回しになってしまう。
        ranked.append((1 if slug in other else 0, counts.get(slug, 0), last, slug, q))
    ranked.sort(key=lambda t: (t[0], t[1], t[2]))
    picked = ranked[:GROUP_QUERY_BATCH]
    try:
        sb.set_last_seen_many([(f"yq:{slug}", today) for *_, slug, _q in picked])
    except Exception as e:
        print(f"  検索日の記録に失敗(次回も同じ順番になります): {e}")
    if picked:
        no_other = sum(1 for p in picked if p[0] == 0)
        thin = sum(1 for p in picked if p[1] < 3)
        print(f"グループ名検索: {len(picked)} 件 "
              f"(他経路なし {no_other} 件 / 今後3件未満 {thin} 件 / 候補 {len(ranked)} 件)")
    names = {i["idol_slug"]: i["name"] for i in idols}
    return [{"idol_slug": slug, "name": names.get(slug, slug), "query": q}
            for *_, slug, q in picked]


# 1回の実行で公式サイトを探すグループ数の上限(候補ごとに取得が入るため)
SITE_DISCOVERY_LIMIT = int(os.environ.get("SITE_DISCOVERY_LIMIT") or 20)

# 公式サイトではないホスト。投稿に出てくるURLの大半はこれらなので、先に落とす。
_NOT_OFFICIAL_HOSTS = (
    "x.com", "twitter.com", "t.co", "fb.me", "facebook.com", "instagram.com",
    "threads.net", "tiktok.com", "youtube.com", "youtu.be", "nicovideo.jp",
    "twitcasting.tv", "showroom-live.com", "mixch.tv", "17.live", "zaiko.io",
    "spotify.com", "apple.com", "linkco.re", "lnk.to", "linktr.ee", "lit.link",
    "line.me", "note.com", "ameblo.jp", "hatenablog.com", "fanicon.net",
    "natalie.mu", "realsound.jp", "barks.jp", "oricon.co.jp", "billboard-japan.com",
    "walkerplus.com", "eiga.com", "prtimes.jp", "google.com", "docs.google.com",
    "forms.gle", "amazon.co.jp", "rakuten.co.jp", "tower.jp", "hmv.co.jp",
    "eplus.jp", "l-tike.com", "pia.jp", "zan-live.com", "shindanmaker.com",
    "timetreeapp.com", "timetr.ee", "wikipedia.org", "idol-", "matome",
)


def _discover_official_sites(other_urls: dict, qmap: dict) -> int:
    """グループ名検索の投稿に出てきたURLから、そのグループの公式サイトを見つける。

    図鑑にリンクが1つも無いグループが200組以上あり、その多くは公式サイトの
    URLが分からないままになっている。チケットサイトの発見で既に投稿は取って
    いるので、そこに出てくる他のURLを見れば検索を増やさずに候補が得られる。

    誤登録を避けるため、ホストのトップページを取って
      ① 本文にグループ名が出てくる(事務所や販売店のサイトを弾く)
      ② 今後の日付がある、またはカレンダーが貼られている
    の両方を満たすものだけを記録する。
    """
    if not other_urls or not qmap:
        return 0
    try:
        have_site = {e["idol_slug"] for e in build_site_watch_list()} | site_skip_slugs()
    except Exception as e:
        print(f"公式サイト発見: 既存の登録を確認できないため中止します: {e}")
        return 0
    found = []
    checked_groups = 0
    for query, urls in other_urls.items():
        g = qmap.get(query)
        if not g or g["idol_slug"] in have_site:
            continue
        if checked_groups >= SITE_DISCOVERY_LIMIT:
            break
        checked_groups += 1
        seen_hosts = set()
        for u in urls:
            try:
                host = urllib.parse.urlparse(u).netloc.lower()
            except Exception:
                continue
            host = host[4:] if host.startswith("www.") else host
            if not host or host in seen_hosts or len(seen_hosts) >= 3:
                continue
            if any(bad in host for bad in _NOT_OFFICIAL_HOSTS):
                continue
            seen_hosts.add(host)
            root = f"https://{host}/"
            # 候補の確認はトップページのHTMLだけで済ませる(サブページ取得やJS描画を
            # 挟むと1組あたり十数秒かかり、発見モードの実行時間に収まらない)。
            try:
                html = sites._get(root)
            except Exception as e:
                print(f"  {g['name']}: 候補 {host} の取得に失敗: {e}")
                continue
            if not html:
                continue
            text = sites._html_to_text(html)
            cal = sites.calendar_links(html)
            if not text:
                continue
            # 本文にグループ名が出てくるだけでは、そのグループを取り上げた記事や
            # ファンのブログも通ってしまう。公式サイトはページの見出し(<title>)に
            # グループ名を置いている(実測7サイトすべて)ので、そこで見る。
            if not _site_mentions_group(g["name"], sites._page_title(html)):
                continue
            if not (sites.has_future_dates(text) or cal):
                print(f"  {g['name']}: 候補 {host} は予定が見当たらないので見送り")
                continue
            print(f"  ★ {g['name']}: 公式サイトらしいサイトを見つけました {root}")
            found.append((f"sitefound:{g['idol_slug']}", root))
            break
    if not found:
        return 0
    try:
        sb.set_last_seen_many(found)
    except Exception as e:
        print(f"公式サイト発見: 記録に失敗: {e}")
        return 0
    print(f"公式サイト発見: {len(found)} 件を巡回対象に追加しました")
    return len(found)


def _artists_match_idol(artists, idol_name: str) -> bool:
    """TicketDive の出演者一覧に idol_name が含まれるか(名前マッチのみ・LLM不使用)。"""
    key = ticketdive._norm_name(idol_name)
    if len(key) < 3:  # 短すぎる名前は誤マッチしやすいので対象外
        return False
    for a in artists or []:
        na = ticketdive._norm_name(a)
        if not na:
            continue
        if na == key or (len(key) >= 5 and key in na) or (len(na) >= 5 and na in key):
            return True
    return False


def _cleanup_past_by_date_suffix(slug: str, today: str, kind: str) -> int:
    """IDの末尾に埋めた開催日が過去になった行を削除する(yrt / ttx 共通)。

    発見が機会依存の行(その日の検索や他グループの告知に出るかどうかで変わる)は、
    今回出力されなかったことを「中止」と解釈できないため、再突合削除はしない。
    """
    removed = 0
    ymd = today.replace("-", "")
    try:
        for old_id in sb.get_auto_event_ids(slug, prefix=f"{kind}:{slug}:"):
            m = re.search(r":(\d{8})$", old_id)
            if m and m.group(1) < ymd:
                sb.delete_auto_event(old_id)
                removed += 1
    except Exception as e:
        print(f"  {kind} の掃除でエラー: {e}")
    return removed


def _cleanup_past_yrt(slug: str, today: str) -> int:
    return _cleanup_past_by_date_suffix(slug, today, "yrt")


def _event_key(source: str, url: str) -> str:
    """イベント行のIDに使う識別子。サイト間で衝突しないよう接頭辞を付ける。"""
    if source == "ticketdive":
        return ticketdive.festival_key(url)
    seg = (url or "").rstrip("/").split("/")[-1].split("?")[0]
    return f"{'lp' if source == 'livepocket' else 'tg'}-{seg}"


def _build_yrt_row(slug: str, source: str, url: str, info: dict, dt: str, multi_day: bool) -> dict:
    # ツアーは日付ごとに会場が違うので、その日の会場を使う(無ければ代表会場)
    venue = info.get("venue")
    open_time = info.get("open_time")
    if multi_day:
        day_stages = info.get("day_stages") or []
        venue = ticketdive.venue_by_date(day_stages).get(dt) or venue
        open_time = ticketdive.open_time_by_date(day_stages).get(dt) or open_time
    return {
        "tweet_id": f"yrt:{slug}:{_event_key(source, url)}:{dt.replace('-', '')}",
        "idol_slug": slug,
        "account": None,
        "event_name": info.get("event_name"),
        "event_datetime": dt if multi_day else normalize_event_datetime(info.get("event_datetime")),
        "open_time": open_time,
        "entry_time": None,
        "sale_datetime": to_timestamptz(info.get("sale_datetime")),
        "is_first_come": bool(info.get("is_first_come")),
        "ticket_url": url,
        "ticket_types": info.get("ticket_types") or [],
        "max_tickets": None,
        "other_details": clean_venue(venue),
        "raw_text": None,
        "tweet_url": url,
    }


def _match_dates(source: str, info: dict, name: str, today: str):
    """このイベントで idol が出演する日付(YYYY-MM-DD)を返す。名前マッチのみ。"""
    if source == "ticketdive":
        day_stages = info.get("day_stages") or []
        if len({d["date"] for d in day_stages}) > 1:
            return ticketdive.match_idol_days(day_stages, name), True
        matched = _artists_match_idol(info.get("artists"), name)
    else:
        matched = _artists_match_idol(info.get("performers"), name)
        if not matched and info.get("act_labeled"):
            # 出演欄が自由文で分割しきれない場合に備え、出演欄テキストへの部分一致も見る
            key = ticketdive._norm_name(name)
            matched = len(key) >= 4 and key in ticketdive._norm_name(info.get("act_text") or "")
    return ([(info.get("event_datetime") or "")[:10]] if matched else []), False


def _yrt_cache_key(source: str, url: str) -> str:
    return f"yrtu:{source}:{_event_key(source, url)}"


# LivePocket の新着一覧を何ページ読むか(1ページ20件)。公開日が新しい順なので
# 前回の巡回以降に公開されたぶんが入っていればよい。
LIVEPOCKET_PAGES = int(os.environ.get("LIVEPOCKET_PAGES") or 3)
# そのうち実際にイベントページを取りに行く上限
LIVEPOCKET_FETCH_LIMIT = int(os.environ.get("LIVEPOCKET_FETCH_LIMIT") or 25)

# 1回の巡回で読み直す、取り込み済みイベントページの上限
YRT_REFRESH_LIMIT = int(os.environ.get("YRT_REFRESH_LIMIT") or 60)


def _source_of_url(url: str):
    if ticketdive.is_ticketdive_url(url):
        return "ticketdive"
    if "livepocket.jp" in url:
        return "livepocket"
    if "tiget.net" in url:
        return "tiget"
    return None


def _add_stored_yrt_urls(by_site: dict) -> int:
    """保存済みの yrt 行が指すチケットページを、今回の取得対象に足す。

    当日の取得済みキャッシュは残すので、同じ日に2回読むことはない。
    毎回同じURLに偏らないよう、最後に読んだ日が古いものから選ぶ。
    """
    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        urls = list(dict.fromkeys(
            r["ticket_url"] for r in sb.get_ticket_page_rows(today_str, prefix="yrt:")
            if r.get("ticket_url")))
    except Exception as e:
        print(f"  取り込み済みURLの取得に失敗(読み直しは行いません): {e}")
        return 0
    if not urls:
        return 0
    try:
        last = sb.get_last_seen_by_prefix("yrtu:")
    except Exception:
        last = {}
    known = {u for v in by_site.values() for u in v}
    cand = [u for u in urls if u not in known and _source_of_url(u)]
    cand.sort(key=lambda u: last.get(_yrt_cache_key(_source_of_url(u), u)) or "")
    added = 0
    for u in cand[:YRT_REFRESH_LIMIT]:
        by_site.setdefault(_source_of_url(u), []).append(u)
        added += 1
    return added


# 1回の巡回で読む、カレンダーから入ったチケットページの上限。
# 初回は700件ほどあるので、数日かけて読む(同じページはその日のうちに読み直さない)。
CALENDAR_URL_LIMIT = int(os.environ.get("CALENDAR_URL_LIMIT") or 80)
# 読んで登録グループが1組も当たらなかったページを、次に読み直すまでの日数
# (出演者は後から追加発表されるので、いつかは読み直す)
CALENDAR_URL_RETRY_DAYS = 7


def _add_calendar_ticket_urls(by_site: dict) -> int:
    """カレンダー(TimeTree・Googleカレンダー)の予定に付いたチケットページを、
    今回の取得対象に足す。

    チケットページを読むと、出演者一覧にいる登録グループ全員にその公演の行が入る。
    これまでは X の検索で見つけたページだけがこの処理を通り、カレンダーから
    入ったページは発売情報を足すだけだった(refresh_sale_info)。そのため対バンでも、
    カレンダーを持つ1組のページにしか公演が出なかった。実測(2026-10-27): 今後の
    予定に付いたページのうち、出演者を読んでいないものが TicketDive 512/535件・
    LivePocket 92/98件・TiGET 105/127件。

    一度読んで行ができたページは _add_stored_yrt_urls が読み直すので、ここでは
    まだ読んでいないもの(または、読んだが登録グループが当たらず、しばらく
    経ったもの)だけを足す。公演日が近いものから。
    """
    now = datetime.datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")
    retry_before = (now - datetime.timedelta(days=CALENDAR_URL_RETRY_DAYS)).strftime("%Y-%m-%d")
    try:
        rows = sb.get_ticket_page_rows(today_str)
    except Exception as e:
        print(f"  カレンダーのチケットページの取得に失敗(読みません): {e}")
        return 0
    try:
        last = sb.get_last_seen_by_prefix("yrtu:")
    except Exception:
        last = {}
    known = {u for v in by_site.values() for u in v}
    soonest: dict = {}
    for r in rows:
        url = r.get("ticket_url")
        # 「t-dv.com/HEROINES-26102…」のように途中で切れた URL は読めない
        if not url or r["tweet_id"].startswith("yrt:") or "…" in url:
            continue
        source = _source_of_url(url)
        if not source or url in known:
            continue
        seen = last.get(_yrt_cache_key(source, url)) or ""
        if seen and seen >= retry_before:
            continue
        date = (r.get("event_datetime") or "")[:10]
        # 「t-dv.com/X」と「ticketdive.com/event/X」は同じページなので1件に数える
        key = _yrt_cache_key(source, url)
        if key not in soonest or date < soonest[key][0]:
            soonest[key] = (date, source, url)
    cand = sorted(soonest.values())
    for _, source, url in cand[:CALENDAR_URL_LIMIT]:
        by_site.setdefault(source, []).append(url)
    n = min(len(cand), CALENDAR_URL_LIMIT)
    if cand:
        print(f"  カレンダーから入ったチケットページ: 未読 {len(cand)} 件のうち {n} 件を読む")
    return n


def _ingest_sale_url(source: str, url: str, idols, today: str, touched: set):
    """チケットページ1件を読み、出演する登録グループぶんの行を書く。

    返り値 (書いた行数, 当日取得済みでスキップしたか)。
    同じイベントは何度も出回るので、当日すでに取り込んだURLは再取得しない
    (高頻度実行でも相手サイトへの負荷と実行時間を増やさないため)。
    """
    cache_key = _yrt_cache_key(source, url)
    force = os.environ.get("YRT_FORCE", "").lower() in ("1", "true")
    try:
        if not force and sb.get_last_seen(cache_key) == today:
            return 0, True
    except Exception:
        pass
    info = _td_info(url) if source == "ticketdive" else ticketsite.fetch_event(url)
    if not info:
        return 0, False
    try:
        sb.set_last_seen(cache_key, today)
    except Exception:
        pass
    batch = []
    for idol in idols:
        name, slug = idol["name"], idol["idol_slug"]
        dates, multi_day = _match_dates(source, info, name, today)
        for dt in dates:
            if not dt or dt < today:
                continue  # 終了済みの公演は取り込まない
            batch.append(_build_yrt_row(slug, source, url, info, dt, multi_day))
            touched.add(slug)
            print(f"  → [{source}] {name}: {info.get('event_name')} ({dt})")
    # フェス1本で100組を超えることがあるので、1行ずつではなくまとめて書く
    try:
        sb.upsert_auto_events(batch)
        return len(batch), False
    except Exception as e:
        print(f"  書き込み失敗({len(batch)}件): {e}")
        return 0, False


def run_livepocket() -> int:
    """LivePocket のアイドルカテゴリ新着一覧から、販売前・販売中のものを取り込む。

    Yahoo!リアルタイム検索は X への投稿を拾う仕組みなので、投稿が無いイベントや
    投稿が発売直前のイベントは間に合わない。一覧を直接見れば公開された時点で拾える。

    一覧に「販売前/販売中」の表示と出演者が載っているため、登録グループが出る
    ものだけに絞ってからイベントページを取りに行く(無駄な取得をしない)。

    販売中も対象にするのは、ライポケは公開から発売までが短く、公開と同時に
    発売が始まるものも多いため(新着60件のうち22件がすでに販売中だった,
    2026-10-27)。販売前だけを拾っていたときは、それらはカレンダー経由で
    見つかるのを待つことになり、ライポケの公演の56%を発売が始まってから
    見つけていた(発売開始から中央値8日後)。読む回数のほうは、ワークフローの
    QUICK_LISTING(1時間ごと)で増やしている。

    LivePocket はデータセンターからの素の HTTP 取得に AWS WAF の認証ページを
    返すが、ticketsite 側で認証ページを受け取ったときだけ実ブラウザで開き直し、
    その後は認証済みのクッキーを使い回すようにしたため一覧も読める
    (実測: 一覧 136,326字 / イベントURL 35件)。
    LIVEPOCKET_LISTING=0 で止められる。
    """
    if os.environ.get("LIVEPOCKET_LISTING") == "0":
        print("LivePocket の一覧巡回は無効化されています(LIVEPOCKET_LISTING=0)")
        return 0
    try:
        cards = ticketsite.livepocket_listing(pages=LIVEPOCKET_PAGES)
    except Exception as e:
        print(f"LivePocket の一覧を取得できません: {e}")
        return 0
    if not cards:
        print("LivePocket の一覧からイベントを取れませんでした。")
        return 0
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    idols = build_idol_name_list()
    lineup_idx = lineup.build_index(idols)
    pre = [c for c in cards if c["status"] == "販売前"]
    on_sale = [c for c in cards if c["status"] == "販売中"]
    # 一覧の出演者欄で登録グループに当たるものだけを対象にする。
    # 取りに行く上限があるので、間に合わせたい販売前を先に並べる。
    targets = []
    for c in pre + on_sale:
        text = f"{c['name']}\n{' / '.join(c['performers'])}"
        if lineup.names_in_text(text, lineup_idx):
            targets.append(c)
    print(f"LivePocket 新着一覧: {len(cards)} 件 / 販売前 {len(pre)} 件・販売中 {len(on_sale)} 件 / "
          f"登録グループが出る {len(targets)} 件")
    total = 0
    touched = set()
    for c in targets[:LIVEPOCKET_FETCH_LIMIT]:
        added, _ = _ingest_sale_url("livepocket", c["url"], idols, today, touched)
        total += added
    removed = sum(_cleanup_past_yrt(slug, today) for slug in touched)
    print(f"LivePocket 取り込み完了: 合計 {total} 件 / 過去分 {removed} 件削除")
    return total


# TiGET のアイドルカテゴリ新着一覧を何ページ読むか(1ページ24件)
TIGET_PAGES = int(os.environ.get("TIGET_PAGES") or 2)
# そのうち実際にイベントページを取りに行く上限
TIGET_FETCH_LIMIT = int(os.environ.get("TIGET_FETCH_LIMIT") or 50)
# 一度読んだイベントページを、何日あけたら読み直すか
TIGET_RECHECK_DAYS = int(os.environ.get("TIGET_RECHECK_DAYS") or 7)


def run_tiget() -> int:
    """TiGET のアイドルカテゴリ新着一覧から、登録グループが出るものを取り込む。

    Yahoo!リアルタイム検索は X への投稿を拾う仕組みなので、投稿が無い公演や
    投稿が発売直前の公演には間に合わない。一覧を直接見れば公開された時点で拾える。

    LivePocket の一覧と違い、TiGET の一覧には出演者が載らず公演名も途中で
    切れているため、出演者を見るにはイベントページを取りに行くしかない。
    そこで一度読んだURLは TIGET_RECHECK_DAYS 日あけるまで読み直さない
    (新着順なので、2回目以降に取りに行くのは新しく出たぶんだけになる)。
    """
    if os.environ.get("TIGET_LISTING") == "0":
        print("TiGET の一覧巡回は無効化されています(TIGET_LISTING=0)")
        return 0
    try:
        cards = ticketsite.tiget_listing(pages=TIGET_PAGES)
    except Exception as e:
        print(f"TiGET の一覧を取得できません: {e}")
        return 0
    if not cards:
        print("TiGET の一覧からイベントを取れませんでした。")
        return 0
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    cutoff = (datetime.datetime.now(JST)
              - datetime.timedelta(days=TIGET_RECHECK_DAYS)).strftime("%Y-%m-%d")
    try:
        last = sb.get_last_seen_by_prefix("yrtu:tiget:")
    except Exception:
        last = {}
    targets = [c for c in cards
               if (last.get(_yrt_cache_key("tiget", c["url"])) or "") < cutoff]
    print(f"TiGET 新着一覧: {len(cards)} 件 / 読み直しの時期でないものを除いて "
          f"{len(targets)} 件")
    idols = build_idol_name_list()
    total = 0
    touched = set()
    for c in targets[:TIGET_FETCH_LIMIT]:
        added, _ = _ingest_sale_url("tiget", c["url"], idols, today, touched)
        total += added
    removed = sum(_cleanup_past_yrt(slug, today) for slug in touched)
    print(f"TiGET 取り込み完了: 合計 {total} 件 / 過去分 {removed} 件削除")
    return total


def run_yahoo_rt(discover_sites: bool = True) -> int:
    """Yahoo! リアルタイム検索で X 上のチケットサイト(TicketDive / LivePocket / TiGET)の
    リンクを発見し、出演者が図鑑の登録グループと一致するイベントを取り込む。

    TicketDive には検索手段が無く、LivePocket / TiGET もグループ単位の巡回は用意が無いため、
    これが対バン/フェスを含むイベントの能動発見経路になる。抽出は構造化データのみで LLM 不要。
    """
    idols = build_idol_name_list()
    if not idols:
        print("登録アイドルを取得できないため Yahoo リアルタイム取り込みをスキップ。")
        return 0
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    groups = _group_search_queries(today)
    qmap = {g["query"]: g for g in groups}
    queries = list(YRT_QUERIES) + [g["query"] for g in groups]
    other_urls: dict = {}
    # クエリ数が増えるため、サイトごとの上限も引き上げる(当日取得済みURLは
    # キャッシュで省かれるので、2回目以降の取得コストは増えない)。
    by_site = yahoo_rt.collect_event_urls(queries, limit_per_site=150,
                                          other_urls=other_urls)
    # 取り込み済みのイベントページを少しずつ読み直す。リアルタイム検索は最近の
    # 投稿しか返さないので、一度取り込んだページは放っておくと二度と読まれず、
    # 販売元がページを直しても古いままになる(ツアーの会場が日程ごとに入った
    # フルコース「Bon Voyage」で、古い行だけ全日程が仙台Rensaのまま残った)。
    refreshed = _add_stored_yrt_urls(by_site)
    # カレンダーの予定に付いたチケットページも、出演者を読んで共演グループに入れる
    from_calendar = _add_calendar_ticket_urls(by_site)

    total_urls = sum(len(v) for v in by_site.values())
    if not total_urls:
        print("Yahooリアルタイム: チケットサイトのリンクは見つかりませんでした。")
        return 0
    print("Yahooリアルタイム: " + " / ".join(f"{k} {len(v)}件" for k, v in by_site.items())
          + (f" / 取り込み済みの読み直し {refreshed}件" if refreshed else "")
          + (f" / カレンダーから {from_calendar}件" if from_calendar else ""))

    total = 0
    touched = set()
    skipped = 0
    for source, urls in by_site.items():
        for url in urls:
            added, was_skipped = _ingest_sale_url(source, url, idols, today, touched)
            total += added
            skipped += 1 if was_skipped else 0

    # 公式サイトの発見は候補ごとに取得が入るので1日1回(フル巡回)だけにする。
    # 発見のみモードは1日3回あり、ここを足すと無料枠(2,000分/月)を超えてしまう。
    if discover_sites:
        _discover_official_sites(other_urls, qmap)

    # 発見は機会依存(今日の検索に出ない=中止ではない)ので再突合削除はせず、
    # 開催日が過ぎた行だけを掃除する。
    removed = sum(_cleanup_past_yrt(slug, today) for slug in touched)
    print(f"Yahooリアルタイム取り込み完了: 合計 {total} 件 / "
          f"当日取得済みスキップ {skipped} 件 / 過去分 {removed} 件削除")
    return total


CURATED_FILE = "curated_events.json"


def run_curated() -> int:
    """curated_events.json に手で書いたイベントを取り込む。

    公式サイトがJS描画や特殊な作りで、LLMでは何度やっても取れないグループがある
    (KiiiKiii・GILTY x GILTY・Panic Monster !n Wonderland など)。そうした分を
    人が書いて足せる経路。ファイルから消せばその行も消える(slug単位で再突合)。

    1行の形:
      {"idol_slug": "artist:KiiiKiii", "event_name": "...",
       "event_datetime": "2026-10-12 18:00", "venue": "...",
       "sale_datetime": "2026-09-01 10:00", "is_first_come": false,
       "ticket_url": "https://...", "source_url": "https://...", "note": "..."}
    event_datetime 以外は省略可。
    """
    try:
        with open(CURATED_FILE, encoding="utf-8") as f:
            entries = json.load(f)
    except FileNotFoundError:
        return 0
    except Exception as e:
        print(f"{CURATED_FILE} の読み込みに失敗: {e}")
        return 0
    if not isinstance(entries, list) or not entries:
        return 0
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        known = {i["idol_slug"] for i in build_idol_name_list()}
    except Exception as e:
        print(f"手入力イベント: 登録グループ一覧の取得に失敗したので中止します: {e}")
        return 0

    rows_by_slug = {}
    skipped = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        slug = (e.get("idol_slug") or "").strip()
        name = (e.get("event_name") or "").strip()
        when = normalize_event_datetime((e.get("event_datetime") or "").strip())
        if not slug or not name or not when:
            print(f"  形が足りない行を飛ばします: {str(e)[:60]}")
            continue
        if slug not in known:
            print(f"  図鑑に無い slug を飛ばします: {slug}")
            continue
        if when[:10] < today:
            skipped += 1
            continue
        key = hashlib.sha1(f"{slug}|{name}|{when}".encode("utf-8")).hexdigest()[:20]
        sale = e.get("sale_datetime")
        row = {
            "tweet_id": f"cur:{slug}:{key}",
            "idol_slug": slug,
            "account": None,
            "event_name": name,
            "event_datetime": when,
            "open_time": e.get("open_time"),
            "entry_time": e.get("entry_time"),
            "sale_datetime": to_timestamptz(normalize_datetime_string(sale) or sale) if sale else None,
            "is_first_come": bool(e.get("is_first_come")),
            "ticket_url": e.get("ticket_url"),
            "ticket_types": e.get("ticket_types") or [],
            "max_tickets": None,
            "other_details": clean_venue(e.get("venue")),
            "raw_text": e.get("note") or None,
            "tweet_url": e.get("source_url") or e.get("ticket_url"),
        }
        rows_by_slug.setdefault(slug, []).append(row)

    total = 0
    for slug, rows in rows_by_slug.items():
        try:
            sb.upsert_auto_events(rows)
        except Exception as e:
            print(f"  {slug}: 書き込み失敗({len(rows)}件): {e}")
            continue
        total += len(rows)
        # ファイルから消した行は消す(このグループの cur: 行を再突合)
        emitted = {r["tweet_id"] for r in rows}
        removed = 0
        try:
            for old_id in sb.get_auto_event_ids(slug, prefix=f"cur:{slug}:"):
                if old_id not in emitted:
                    sb.delete_auto_event(old_id)
                    removed += 1
        except Exception as e:
            print(f"  {slug}: 再突合でエラー: {e}")
        print(f"  {slug}: {len(rows)} 件反映 / {removed} 件削除")
    # ファイルから全部消したグループの行も消す
    try:
        for r in sb.get_auto_event_rows_by_prefix("cur:"):
            if r.get("idol_slug") not in rows_by_slug:
                sb.delete_auto_event(r["tweet_id"])
    except Exception as e:
        print(f"  不要になった手入力行の掃除でエラー: {e}")
    print(f"手入力イベント取り込み完了: 合計 {total} 件"
          + (f" / 終了済み {skipped} 件は対象外" if skipped else ""))
    return total


def _ttx_source_slug(tweet_id: str, idol_slug: str) -> Optional[str]:
    """ttx 行のIDから、元になったカレンダーの持ち主 slug を取り出す。

    ID は ttx:<反映先slug>:<元slug>:<イベントID>:<YYYYMMDD>。slug 自体が
    "artist:名前" でコロンを含むので、前は反映先slug、後ろは2区切りで落とす。
    """
    head = f"ttx:{idol_slug}:"
    if not tweet_id.startswith(head):
        return None
    body = tweet_id[len(head):]
    parts = body.rsplit(":", 2)
    return parts[0] if len(parts) == 3 else None


# ---------------------------------------------------------- リリイベ巡回

# タワーレコードの店舗イベント一覧を何ページ読むか(1ページ20件)
RELEASE_TOWER_PAGES = int(os.environ.get("RELEASE_TOWER_PAGES") or 14)
# HMVの店舗イベント一覧を何ページ読むか
RELEASE_HMV_PAGES = int(os.environ.get("RELEASE_HMV_PAGES") or 2)
# 1回の巡回で詳細ページを読む上限(整理番号とチケットリンクはここでしか分からない)
# 実測: 詳細120件で約3.5分。1回の巡回の持ち時間(55分)には余裕があるので、
# 対象(実測490件)を2回で一周できるところまで上げる
RELEASE_DETAIL_LIMIT = int(os.environ.get("RELEASE_DETAIL_LIMIT") or 250)
# 一度読んだ詳細ページを、何日あけたら読み直すか
RELEASE_RECHECK_DAYS = int(os.environ.get("RELEASE_RECHECK_DAYS") or 5)
# ヴィレヴァンの詳細は1件ごとにブラウザが要るので、1回に読む本数を絞る
# (ブラウザで開ける上限はLivePocketと分け合うため)
RELEASE_VV_LIMIT = int(os.environ.get("RELEASE_VV_LIMIT") or 15)


def _rel_key(url: str) -> str:
    return "rel:" + url


def _days_since(day: Optional[str], today: str) -> int:
    """YYYY-MM-DD の差。読んだ記録が無ければ大きい数を返す。"""
    if not day:
        return 9999
    try:
        a = datetime.datetime.strptime(day, "%Y-%m-%d").date()
        b = datetime.datetime.strptime(today, "%Y-%m-%d").date()
        return (b - a).days
    except ValueError:
        return 9999


def _release_slug(card: dict, detail: Optional[dict], lineup_idx):
    """出演欄・イベント名から図鑑のグループを探す。見つからなければ None。

    リリイベは女性アイドルに限らず集めるので、図鑑に無くても行は作る。
    slug が付いた行だけが、お気に入りの出演にも出る。
    """
    text = "\n".join(filter(None, [
        card.get("artist"), (detail or {}).get("cast"), card.get("name"),
    ]))
    names = lineup.names_in_text(text, lineup_idx) if text else []
    return names[0][1] if names and isinstance(names[0], (list, tuple)) else None


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _real_release_name(name: Optional[str]) -> bool:
    """公演名として使える名前か。一覧の注意書き(「・」始まり)や空の名前を外す。"""
    n = (name or "").strip()
    return len(n) >= 2 and not n.startswith(("・", "※", "＊", "*"))


def _release_place(name: Optional[str], store: Optional[str]) -> Optional[str]:
    """会場。公演名に別の場所が書かれていればそちらを採る。

    タワーレコードの一覧は、ほかの場所で開く催しも近くの店の欄に載せる
    (「…全国行脚＠アミュプラザ長崎（長崎）」が博多店の欄にある)。
    店の名前を含む書き方(「@タワーレコードアミュプラザ博多店」)はその店のまま。
    """
    # 名前の末尾の「＠場所（県）」だけを見る。「@JAM EXPO 出演記念」のような名前の一部は場所ではない
    m = re.search(r"[@＠]\s*([^@＠()（）【】「」『』]{2,30}?)\s*(?:[（(][^)）]{1,10}[)）])?\s*(?:【[^】]{1,12}】\s*)*$", name or "")
    if not m or not store or m.start() == 0:
        return store
    place = m.group(1).strip()
    if re.match(r"(?i)jam\b", place):
        return store  # 「@JAM」はイベント名
    norm = lambda x: re.sub(r"[\s・]", "", x)
    if norm(store) in norm(place) or norm(place) in norm(store):
        return store
    return place


def _drop_broken_release_rows():
    """以前の取り込みで入った、日付が「開催日未定」の行や注意書きの行を消す。"""
    try:
        old = sb.get_auto_event_rows_by_prefix("rel:")
    except Exception as e:
        print(f"  リリイベの既存行を読めません: {e}")
        return
    bad = [r["tweet_id"] for r in old
           if not re.search(r":\d{8}", r["tweet_id"]) or not _real_release_name(r.get("event_name"))]
    if bad:
        sb.delete_auto_events(bad)
        print(f"  日付や名前が読めていなかったリリイベの行を {len(bad)} 件消しました")


def _release_row(card: dict, detail: Optional[dict], slug, ticket: Optional[dict]) -> dict:
    """リリイベ1件を auto_events の行にする。

    リリイベ固有の中身(整理番号の出し方・出演者名)を入れる列は無いので、
    既にある列だけで収める。auto_events への列追加はダッシュボードからしか
    できず、取り込みワーカーの持つキーでは実行できないため。
      account  : "release:tower" のような取り込み元。一覧はこれで絞る
      raw_text : {"numbering","performer","note"} の JSON
    """
    d = detail or {}
    date = card.get("date") or d.get("date")
    start = card.get("time") or d.get("time")
    num = (d.get("numbering") or {})
    # 事前販売と言い切れるのは、受付開始の分かる販売ページがあるときだけ。
    # 文面だけで「事前」と読むと当日配布のものまで拾ってしまう(実測で
    # 9件中4件が当日配布だった)。LINEアプリ発券は当日その場ではないので別扱い。
    numbering = None
    if num.get("numbered"):
        if ticket and ticket.get("sale_datetime"):
            numbering = "advance"
        elif num.get("line"):
            numbering = "line"
        elif num.get("advance"):
            numbering = "advance"
        else:
            numbering = "onsite"
    ticket_url = (d.get("ticket_urls") or [None])[0]
    note = {
        "numbering": numbering,
        "performer": card.get("artist") or d.get("cast") or None,
        "note": num.get("lines") or [],
    }
    return {
        # 同じイベントページが「10/16 14:00 / 16:30 / 18:30」のように
        # 回ごとに並ぶ(実測: 80行中11ページが重複)。日時まで入れて1回=1行にする
        "tweet_id": (f"rel:{card['source']}:{card['url'].rstrip('/').rsplit('/', 1)[-1]}"
                     f":{(date or '').replace('-', '')}{(start or '').replace(':', '')}"),
        "idol_slug": slug,
        "account": f"release:{card['source']}",
        "event_name": card.get("name"),
        "event_datetime": date,
        "open_time": start,
        "entry_time": None,
        "sale_datetime": to_timestamptz((ticket or {}).get("sale_datetime")),
        "is_first_come": bool((ticket or {}).get("is_first_come")),
        "ticket_url": ticket_url,
        "ticket_types": (ticket or {}).get("ticket_types") or [],
        "max_tickets": None,
        "other_details": clean_venue(_release_place(card.get("name"), card.get("store") or d.get("place"))),
        "raw_text": json.dumps(note, ensure_ascii=False),
        "tweet_url": card["url"],
    }


def run_release() -> int:
    """店舗のリリースイベント(インストアイベント)を取り込む。

    タワーレコード・HMV・ヴィレッジヴァンガードの店舗イベント一覧を読み、
    詳細ページから整理番号の案内と、事前に整理番号を売るチケットページを拾う。
    女性アイドルに限らず集めるので、図鑑に無いアーティストの行も作る
    (図鑑に当たったものだけ idol_slug が付き、お気に入りの出演にも出る)。

    詳細ページは件数が多いので、開催日の近い順に RELEASE_DETAIL_LIMIT 件ずつ、
    一度読んだものは RELEASE_RECHECK_DAYS 日あけて読み直す。
    RELEASE_LISTING=0 で止められる。
    """
    if os.environ.get("RELEASE_LISTING") == "0":
        print("リリイベの巡回は無効化されています(RELEASE_LISTING=0)")
        return 0
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    cards = []
    for name, fn in (
        ("タワーレコード", lambda: release.tower_listing(pages=RELEASE_TOWER_PAGES)),
        ("HMV", lambda: release.hmv_listing(pages=RELEASE_HMV_PAGES)),
        ("ヴィレヴァン", release.vv_listing),
    ):
        try:
            got = fn()
        except Exception as e:
            print(f"  {name}の一覧を取得できません: {type(e).__name__} {e}")
            continue
        print(f"  {name}: {len(got)} 件")
        cards += got
    if not cards:
        print("リリイベ: 一覧からイベントを取れませんでした。")
        return 0

    # 終わった公演は入れない。日付が一覧に無いもの(ヴィレヴァン)は詳細で判断する
    cards = [c for c in cards if not c.get("date") or c["date"] >= today]
    try:
        last = sb.get_last_seen_by_prefix("rel:")
    except Exception:
        last = {}
    # 開催日の近い順。日付が分からないものは後回しにせず先に読む(終了判定のため)
    cards.sort(key=lambda c: c.get("date") or "")
    # 読み取りの直し方を変えたときは、待たずに全部読み直せるようにしておく
    force = os.environ.get("RELEASE_FORCE", "").lower() in ("1", "true")
    due = [c for c in cards
           if force or _days_since(last.get(_rel_key(c["url"])), today) >= RELEASE_RECHECK_DAYS]
    print(f"リリイベ: 今後の公演 {len(cards)} 件 / 詳細を読む {min(len(due), RELEASE_DETAIL_LIMIT)} 件"
          f"(読み直しの時期でないもの {len(cards) - len(due)} 件)")

    lineup_idx = lineup.build_index(build_idol_name_list())
    rows, read, matched, with_ticket = [], 0, 0, 0
    seen_detail: dict = {}  # 同じページが回ごとに並ぶので、1回の巡回では1度だけ読む
    vv_read = 0
    for card in due[:RELEASE_DETAIL_LIMIT]:
        url = card["url"]
        if url in seen_detail:
            detail = seen_detail[url]
        else:
            if card["source"] == "vv":
                if vv_read >= RELEASE_VV_LIMIT:
                    continue  # 残りは次の巡回で
                vv_read += 1
            read += 1
            detail = None
            try:
                detail = release.fetch_detail(url, render=(card["source"] == "vv"))
            except Exception as e:
                print(f"    詳細の取得に失敗 {url[:60]}: {type(e).__name__} {e}")
            seen_detail[url] = detail
            try:
                sb.set_last_seen(_rel_key(url), today)
            except Exception:
                pass
        # 一覧の日付欄が「開催日未定」のこともあるので、日付の形のものだけを採る
        date = next((d for d in (card.get("date"), (detail or {}).get("date"))
                     if d and _ISO_DATE.match(d)), None)
        if not date or date < today:
            continue  # 終了済み、または日付が読めないものは入れない
        if not _real_release_name(card.get("name")):
            continue  # 一覧の注意書きの行(「・イベント券を紛失/盗難…」)を公演と読んだもの
        card["date"] = date
        # 事前に整理番号や参加券を売るページがあれば、受付開始を読む
        ticket = None
        for url in (detail or {}).get("ticket_urls", [])[:1]:
            source = _source_of_url(url)
            if not source:
                continue
            try:
                ticket = _td_info(url) if source == "ticketdive" else ticketsite.fetch_event(url)
            except Exception as e:
                print(f"    チケットページを読めません {url[:60]}: {type(e).__name__} {e}")
            if ticket:
                with_ticket += 1
        slug = _release_slug(card, detail, lineup_idx)
        if slug:
            matched += 1
        rows.append(_release_row(card, detail, slug, ticket))

    # 念のため、同じ tweet_id が2つ以上あれば落とす
    # (PostgREST は同じキーを含む一括 upsert を 500 で返す)
    _drop_broken_release_rows()
    uniq = {r["tweet_id"]: r for r in rows}
    if len(uniq) != len(rows):
        print(f"  同じキーの行を {len(rows) - len(uniq)} 件まとめました")
    rows = list(uniq.values())
    try:
        sb.upsert_auto_events(rows)
    except Exception as e:
        print(f"リリイベの書き込みに失敗({len(rows)}件): {e}")
        return 0
    notes = [json.loads(r["raw_text"]) for r in rows]
    numbered = sum(1 for n in notes if n.get("numbering"))
    advance = sum(1 for n in notes if n.get("numbering") in ("advance", "line"))
    print(f"リリイベ取り込み完了: {len(rows)} 件 / 読んだ詳細 {read} 件 / "
          f"図鑑に当たった {matched} 件 / 整理番号あり {numbered} 件"
          f"(うち当日より前に動くもの {advance} 件) / 事前販売ページを読めた {with_ticket} 件")
    return len(rows)


# 1回の巡回で発売情報を入れ直すチケットページの上限。
# 巡回は1日1回なので、40件だと対象470件を一周するのに10日以上かかり、
# 入ったばかりのリンクは何日も発売日時が空のままだった(2026-10-14の巡回でも
# 40件読んで未着手が76件残っている)。40件の読み込みは実測38秒なので、
# 一周を1〜2回の巡回で終えられるところまで上げる。
SALE_REFRESH_LIMIT = int(os.environ.get("SALE_REFRESH_LIMIT") or 250)


def share_sale_info(rows: list) -> int:
    """同じ販売ページを指す行どうしで、発売情報をそろえる(ページは読まない)。

    発売情報の入れ直しは「同じURLは1日1回」読む。そのため、読んだあとに
    同じ公演を指す行が増えると(新しく足したカレンダー、対バンの相互反映)、
    その行は翌日まで発売日時が空のままになり、先着の一覧に出ない。
    実測(2026-10-27): ライポケのリンクがある今後の行300件のうち、同じURLの
    別の行は発売日時を持っているのに空のままの行が38件(12ページ)あった。
    グループのページにはライポケのリンクが出るのに先着の一覧には出ない、
    という食い違いになる。

    すでに分かっている情報を写すだけなので、ネットに取りに行かず、
    何回実行しても負担が無い。yrt 行は Yahoo!リアルタイム検索の取り込みで
    最新になるので写す元には使うが、書き換えない。
    """
    by_url: dict = {}
    for r in rows:
        if r.get("ticket_url"):
            by_url.setdefault(r["ticket_url"], []).append(r)
    shared = 0
    for url, rs in by_url.items():
        src = [r for r in rs if r.get("sale_datetime")]
        dst = [r for r in rs if not r.get("sale_datetime") and not r["tweet_id"].startswith("yrt:")]
        if not src or not dst:
            continue
        # 写す元どうしで値が違うときは、いちばん多い値を採る
        counts: dict = {}
        for r in src:
            counts[r["sale_datetime"]] = counts.get(r["sale_datetime"], 0) + 1
        sale = max(counts, key=counts.get)
        base = next(r for r in src if r["sale_datetime"] == sale)
        patch = {"sale_datetime": sale, "is_first_come": bool(base.get("is_first_come"))}
        if base.get("ticket_types"):
            patch["ticket_types"] = base["ticket_types"]
        for r in dst:
            try:
                sb.patch_auto_event(r["tweet_id"], patch)
            except Exception as e:
                print(f"  発売情報を写すのに失敗 {r['tweet_id'][:40]}: {e}")
                continue
            r.update(patch)  # このあとの読む順番の決定に反映する
            shared += 1
    if shared:
        print(f"同じ販売ページの行に発売情報を写した: {shared} 行")
    return shared


def refresh_sale_info() -> int:
    """カレンダー由来の行に、販売ページから発売日時・先着・券種を入れる。

    TimeTree のノートや Googleカレンダーには「いつ発売か」が書かれていない
    ので、リンクだけがある行は先着の一覧に出せない(今後の行が指す対応サイトの
    URL471件のうち、発売情報を持つのは Yahoo!リアルタイム検索から入った48件
    だけだった)。ここでは行を増やさず、同じURLを指す行の発売情報だけを
    書き足す。公演名や会場はカレンダー側のほうが実態に合うので触らない
    (行を増やすと同じ公演の枠が2つ出てしまう)。

    発売情報がまだ無いURLを優先し、最後に読んだ日が古い順に少しずつ読む。
    """
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        rows = sb.get_ticket_page_rows(today)
    except Exception as e:
        print(f"  発売情報の入れ直しに失敗(行の取得): {e}")
        return 0
    # ページを読む前に、分かっている情報を同じURLの行へ写しておく。
    # 写せた行は「発売情報あり」になるので、読む順番でも後ろに回る。
    share_sale_info(rows)
    by_url: dict = {}
    for r in rows:
        url = r.get("ticket_url")
        if not url or r["tweet_id"].startswith("yrt:") or not _source_of_url(url):
            continue  # yrt 行は通常の取り込みで最新になる
        by_url.setdefault(url, []).append(r)
    if not by_url:
        return 0
    try:
        last = sb.get_last_seen_by_prefix("sale:")
    except Exception:
        last = {}
    # 発売がこれからのページを最優先にする。先着の一覧に出るのはこの行で、
    # カレンダーのノートから入った「先着かどうか」が外れていることがある
    # (実測: これから発売の8ページのうち3ページは、ページには先着と書いてある
    # のに False のままだった)。次に、発売情報がまだ無いもの。同じ条件なら
    # 読んだ日が古い順。
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _sale_upcoming(u: str) -> bool:
        return any((r.get("sale_datetime") or "") > now_iso for r in by_url[u])

    cand = sorted(
        by_url,
        key=lambda u: (not _sale_upcoming(u),
                       all(r.get("sale_datetime") for r in by_url[u]),
                       last.get(f"sale:{u}") or ""),
    )
    # 当日すでに読んだものは上限を数える前に外す。あとで外すと、上限の枠を
    # 読まないURLで使い切ってしまう(1回の巡回で40件読むつもりが0件になっていた)。
    force = os.environ.get("SALE_FORCE", "").lower() in ("1", "true")
    only = [u.strip() for u in os.environ.get("SALE_URLS", "").split(",") if u.strip()]
    if only:
        # 指定したページだけを読み直す(入らないページの切り分け用)
        cand = [u for u in cand if u in set(only)]
        missing = [u for u in only if u not in by_url]
        if missing:
            print(f"  指定されたが対象に無いURL: {len(missing)} 件 {missing[:3]}")
    elif not force:
        cand = [u for u in cand if last.get(f"sale:{u}") != today]
    if not cand:
        return 0
    print("  先に読む販売ページ: "
          + " / ".join(f"{u.split('/')[-1][:24]}({len(by_url[u])}行)" for u in cand[:8]))
    filled = read = failed = nosale = 0
    for url in cand[:SALE_REFRESH_LIMIT]:
        read += 1
        if only:
            print(f"  読む: {url[:70]} ({len(by_url[url])}行)")
        source = _source_of_url(url)
        info = _td_info(url) if source == "ticketdive" else ticketsite.fetch_event(url)
        try:
            sb.set_last_seen(f"sale:{url}", today)
        except Exception:
            pass
        if not info:
            failed += 1
            continue
        patch = {"is_first_come": bool(info.get("is_first_come"))}
        sale = to_timestamptz(info.get("sale_datetime"))
        if sale:
            patch["sale_datetime"] = sale
        else:
            # 読めたのに発売日時が無いことが続くと原因が分からないので残す。
            # 発売日時は上書きしないが、先着かどうかと券種はページで分かるので
            # 書き足す(ノート由来の誤りがここで直る)。
            nosale += 1
            print(f"    発売日時が見つかりません: {url[:70]}")
        types = info.get("ticket_types") or []
        if types:
            patch["ticket_types"] = types
        for r in by_url[url]:
            try:
                sb.patch_auto_event(r["tweet_id"], patch)
                filled += 1
            except Exception as e:
                print(f"  発売情報の書き込みに失敗 {r['tweet_id'][:40]}: {e}")
    print(f"発売情報を入れ直し: {filled} 行 / 読んだページ {read} 件"
          f"(取得できず {failed} 件 / 発売日時なし {nosale} 件)"
          f" / 未着手 {max(0, len(cand) - read)} 件")
    return filled


def fix_ticket_urls() -> int:
    """保存済みの ticket_url に混ざったゴミを落とす。

    TimeTree のノートや Googleカレンダーの説明文からURLを拾う経路では、
    末尾に HTML の残り("> など)や見えない文字が付くことがあった。抽出側は
    直したが、すでに書かれた行は残るのでここで洗う(174行で確認)。
    """
    try:
        rows = sb.get_ticket_url_rows()
    except Exception as e:
        print(f"  ticket_url の点検に失敗: {e}")
        return 0
    fixed = 0
    for r in rows:
        url = r.get("ticket_url")
        clean = sb.clean_url(url)
        if clean == url:
            continue
        try:
            sb.patch_auto_event(r["tweet_id"], {"ticket_url": clean})
            fixed += 1
        except Exception as e:
            print(f"  URL の書き換えに失敗 {r['tweet_id'][:40]}: {e}")
    if fixed:
        print(f"ticket_url を修復: {fixed} 件 / {len(rows)} 件中")
    return fixed


def cleanup_orphan_rows() -> int:
    """図鑑からリンクを外したグループの行を掃除する。

    各 run_* の再突合削除は「今回巡回した slug」しか見ないため、図鑑から
    TimeTree/Googleカレンダー/公式サイトのリンクを外すと、その slug は巡回
    対象から消え、過去に取り込んだ行が残り続ける。誤って別グループの
    カレンダーを登録していた場合(Appare! に appare☆ず のカレンダーなど)、
    リンクを直しても古い予定が残ってしまうので、ここでまとめて消す。

    ttx(対バンの相互反映)も、元のカレンダーが無くなったなら消す。
    """
    try:
        tt_slugs = {e["idol_slug"] for e in build_timetree_watch_list()}
        gc_slugs = {e["idol_slug"] for e in build_gcal_watch_list()}
        site_slugs = {e["idol_slug"] for e in build_site_watch_list()} | site_skip_slugs()
    except Exception as e:
        print(f"孤児行の掃除: 監視対象の取得に失敗したので中止します: {e}")
        return 0
    # 監視対象が空(カタログ取得の失敗など)のときに全消しをしないための歯止め。
    if not tt_slugs or not site_slugs:
        print("孤児行の掃除: 監視対象が空なので中止します")
        return 0
    # ttx(相互反映)の元になり得るのは tt / gc / site の行なので、どれかに
    # 残っていれば孤児ではない。
    cal_slugs = tt_slugs | gc_slugs | site_slugs
    try:
        known = {i["idol_slug"] for i in build_idol_name_list()}
    except Exception as e:
        print(f"孤児行の掃除: 登録グループ一覧の取得に失敗したので中止します: {e}")
        return 0
    if not known:
        print("孤児行の掃除: 登録グループ一覧が空なので中止します")
        return 0

    doomed = []
    for prefix, keep in (("tt:", tt_slugs), ("gc:", gc_slugs), ("site:", site_slugs)):
        try:
            rows = sb.get_auto_event_rows_by_prefix(prefix)
        except Exception as e:
            print(f"孤児行の掃除({prefix}): 読み込みに失敗: {e}")
            continue
        gone = [r["tweet_id"] for r in rows if r.get("idol_slug") not in keep]
        if gone:
            slugs = sorted({r["idol_slug"] for r in rows if r.get("idol_slug") not in keep})
            print(f"  {prefix} 孤児 {len(gone)} 件 / {len(slugs)} グループ: {', '.join(slugs[:8])}")
        doomed.extend(gone)

    try:
        rows = sb.get_auto_event_rows_by_prefix("ttx:")
    except Exception as e:
        print(f"孤児行の掃除(ttx:): 読み込みに失敗: {e}")
        rows = []
    gone = []
    for r in rows:
        src = _ttx_source_slug(r.get("tweet_id") or "", r.get("idol_slug") or "")
        # 古い版が作った ttx 行は、元が公式サイトやチケットサイト発見のときもあり
        # IDから slug を取り出せない。確実に「カレンダーの持ち主だった slug」と
        # 読めるものだけを消し、読めない行は開催日が過ぎたときの掃除に任せる。
        if src and src in known and src not in cal_slugs:
            gone.append(r["tweet_id"])
    if gone:
        print(f"  ttx: 元カレンダーが外れた行 {len(gone)} 件")
    doomed.extend(gone)

    # 券種ラベルだけの e+ 行(「一般発売」など)。EXTRACT_VERSION を上げたので
    # 次の巡回でも消えるが、巡回の順番待ちで残るため先に落とす。
    try:
        for r in sb.get_auto_event_rows_by_prefix("ep:"):
            if eplus.is_sale_label(r.get("event_name") or ""):
                doomed.append(r["tweet_id"])
    except Exception as e:
        print(f"孤児行の掃除(ep:): 読み込みに失敗: {e}")

    if not doomed:
        print("孤児行の掃除: 対象なし")
        return 0
    try:
        sb.delete_auto_events(doomed)
    except Exception as e:
        print(f"孤児行の掃除: 削除に失敗: {e}")
        return 0
    print(f"孤児行の掃除: {len(doomed)} 件削除")
    return len(doomed)


def main() -> int:
    # X_ONLY=true は twikit(X取り込み)の動作確認用。サイト巡回/TimeTreeを飛ばしXだけ実行する。
    x_only = os.environ.get("X_ONLY", "").lower() == "true"
    # DISCOVERY_ONLY=true はチケットサイト発見だけを回す軽量モード(数分)。
    # 重いサイト巡回(Groqのレート順守で約30分)を挟まずに済むので、高頻度で実行できる。
    discovery_only = os.environ.get("DISCOVERY_ONLY", "").lower() == "true"
    # QUICK_LISTING=true は、ライポケ・TiGET の新着一覧だけを読むいちばん軽いモード
    # (1時間ごと)。ライポケは公開から発売までが短く、1日1回のフル巡回だけで
    # 一覧を読んでいたときは、ライポケの公演の56%を発売が始まってから見つけていた
    # (2026-10-27 実測)。同じイベントページはその日のうちは読み直さない
    # (_ingest_sale_url のキャッシュ)ので、回数を増やしても相手サイトへの取得は
    # 一覧の数ページぶんしか増えない。
    if os.environ.get("QUICK_LISTING", "").lower() == "true":
        run_livepocket()
        run_tiget()
        # 新しく入った行と、同じ販売ページを指す既存の行の発売情報をそろえる
        # (ネットに取りに行かない)
        try:
            share_sale_info(sb.get_ticket_page_rows(
                datetime.datetime.now(JST).strftime("%Y-%m-%d")))
        except Exception as e:
            print(f"  発売情報をそろえられませんでした: {e}")
        return 0
    if discovery_only:
        run_yahoo_rt(discover_sites=False)
        refresh_sale_info()
        # URLの修復は1回洗えば終わりだが、点検自体は数回の読み取りで済むので
        # 軽量モードでも回す(新しく混ざった分をその日のうちに直せる)。
        fix_ticket_urls()
        # 掃除は読み書きが数回で済むので、軽量モードでも回す。図鑑のリンクを
        # 直した日に、フル巡回(1日1回)を待たずに古い予定が消えるようにする。
        cleanup_orphan_rows()
        return 0
    # SITE_ONLY / GCAL_ONLY は、その取り込みだけを回す確認用(数分)。
    if os.environ.get("SITE_ONLY", "").lower() == "true":
        run_sites()
        # 手で書いた公演(curated_events.json)も取り込む。足したあと、1日1回の巡回を
        # 待たずに手動実行で反映できるように(数秒で終わる)
        run_curated()
        return 0
    if os.environ.get("GCAL_ONLY", "").lower() == "true":
        run_gcal()
        return 0
    if os.environ.get("LIVEPOCKET_ONLY", "").lower() == "true":
        run_livepocket()
        return 0
    if os.environ.get("TIGET_ONLY", "").lower() == "true":
        run_tiget()
        return 0
    # 店舗のリリースイベントだけを巡回する(数分)。
    if os.environ.get("RELEASE_ONLY", "").lower() == "true":
        run_release()
        return 0
    # 集めてあるリンクから発売日時・先着・券種を入れ直すだけのモード。
    # 先着チケット一覧の中身を一気に埋めたいときに使う。
    if os.environ.get("SALE_ONLY", "").lower() == "true":
        refresh_sale_info()
        return 0
    # 一覧ページに実行環境からアクセスできるかの確認だけを行う(数秒)
    if os.environ.get("LISTING_CHECK", "").lower() == "true":
        print("チケットサイトの一覧ページを確認します")
        extra = os.environ.get("LISTING_CHECK_URLS", "").strip()
        ticketsite.listing_check([u.strip() for u in extra.split(",") if u.strip()] or None)
        return 0
    if not x_only:
        run_sites()
        run_timetree()
        run_gcal()
        run_eplus()
        run_curated()
        run_yahoo_rt()
        run_livepocket()
        run_tiget()
        run_release()
        refresh_sale_info()
        fix_ticket_urls()
        cleanup_orphan_rows()
    if x_only or os.environ.get("ENABLE_X_INGEST") == "1":
        asyncio.run(run_x())
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        # ボット対策の迂回でブラウザを起動していたら閉じる
        ticketsite.close_browser()
    sys.exit(code)
