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
import re
import traceback
from typing import List, Optional
from pydantic import BaseModel, Field

import sb  # Supabase 入出力(標準ライブラリのみ)
import sites  # 公式サイトからの取得エンジン(主軸)
import timetree  # TimeTree 公開カレンダーからの取得エンジン
import ticketdive  # TicketDive イベントページの構造化パーサ(チケット情報の高精度化)
from watchlist import build_site_watch_list, build_timetree_watch_list, build_watch_list

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


def normalize_datetime_string(raw_text: str) -> Optional[str]:
    if not raw_text:
        return None
    try:
        m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日\s]*[^\d\s]*\s*(\d{1,2})[：:](\d{1,2})", raw_text)
        if m:
            y, mo, d, h, mi = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(h):02d}:{int(mi):02d}:00"
        m = re.search(r"(\d{1,2})[-/月](\d{1,2})[日\s]*[^\d\s]*\s*(\d{1,2})[：:](\d{1,2})", raw_text)
        if m:
            mo, d, h, mi = m.groups()
            y = datetime.datetime.now().year
            return f"{y:04d}-{int(mo):02d}-{int(d):02d} {int(h):02d}:{int(mi):02d}:00"
    except Exception as e:
        print(f"normalize error '{raw_text}': {e}")
    return None


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
    out = []
    for dt in days:
        r = dict(row)
        r["ticket_types"] = list(row.get("ticket_types") or [])
        r["event_name"] = name
        r["event_datetime"] = dt
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
        row["other_details"] = info["venue"]
    return row


def build_row(tweet, username: str, idol_slug: str, info: dict) -> dict:
    open_t, entry_t = timetree.extract_open_entry(tweet.text)
    return {
        "tweet_id": str(tweet.id),
        "idol_slug": idol_slug,
        "account": username,
        "event_name": info.get("event_name"),
        "event_datetime": info.get("event_datetime"),
        "open_time": open_t,
        "entry_time": entry_t,
        "sale_datetime": to_timestamptz(info.get("sale_datetime")),
        "is_first_come": bool(info.get("is_first_come")),
        "ticket_url": info.get("ticket_url"),
        "ticket_types": info.get("ticket_types") or [],
        "max_tickets": info.get("max_tickets"),
        "other_details": info.get("other_details"),
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


def build_site_row(slug: str, name: str, site_url: str, ev) -> Optional[dict]:
    """公式サイト由来の SiteEvent を auto_events 行に変換。tweet_id は合成した安定IDで重複排除。"""
    if not ev.event_name:
        return None
    sale = None
    if ev.sale_datetime:
        sale = to_timestamptz(normalize_datetime_string(ev.sale_datetime) or ev.sale_datetime)
    key = hashlib.sha1(
        f"{slug}|{ev.event_name}|{ev.event_datetime}|{ev.sale_datetime}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "tweet_id": f"site:{slug}:{key}",
        "idol_slug": slug,
        "account": None,
        "event_name": ev.event_name,
        "event_datetime": ev.event_datetime,
        "open_time": ev.open_time,
        "entry_time": ev.entry_time,
        "sale_datetime": sale,
        "is_first_come": bool(ev.is_first_come),
        "ticket_url": ev.ticket_url,
        "ticket_types": [t.model_dump() for t in ev.ticket_types],
        "max_tickets": None,
        "other_details": ev.venue,
        "raw_text": None,
        "tweet_url": site_url,
    }


def run_sites() -> int:
    """公式サイトを巡回してイベント/チケット情報を取り込む(主軸)。"""
    watch = build_site_watch_list()
    if not watch:
        print("公式サイトの監視対象がありません(図鑑に公式サイトURLが未登録の可能性)。")
        return 0
    print(f"公式サイト監視対象: {len(watch)} 件")
    total = 0
    for entry in watch:
        slug, name, url = entry["idol_slug"], entry["name"], entry["site_url"]
        print(f"{name} の公式サイトを確認: {url}")
        try:
            content_hash, events = sites.fetch_site_events(gemini_client, slug, name, url)
        except Exception as e:
            print(f"  失敗: {e}")
            continue
        if content_hash is None:
            continue
        state_key = f"site:{slug}"
        if sb.get_last_seen(state_key) == content_hash:
            print("  変更なし。スキップ")
            continue
        count = 0
        for ev in events:
            row = build_site_row(slug, name, url, ev)
            if row:
                enrich_row_with_ticketdive(row)
                try:
                    sb.upsert_auto_event(row)
                    count += 1
                except Exception as e:
                    print(f"  書き込み失敗: {e}")
        sb.set_last_seen(state_key, content_hash)
        print(f"  {count} 件のイベントを反映")
        total += count
    print(f"公式サイト取り込み完了: 合計 {total} 件")
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
        "event_datetime": ev.get("event_datetime"),
        "open_time": open_t,
        "entry_time": entry_t,
        "sale_datetime": sale,
        "is_first_come": bool(ev.get("is_first_come")),
        "ticket_url": ev.get("ticket_url"),
        "ticket_types": [],
        "max_tickets": None,
        "other_details": ev.get("venue"),
        "raw_text": ev.get("note") or None,
        "tweet_url": ev.get("page_url") or tt_url,
    }


def run_timetree() -> int:
    """TimeTree 公開カレンダーを巡回してイベント/チケット情報を取り込む。"""
    watch = build_timetree_watch_list()
    if not watch:
        print("TimeTree の監視対象がありません(図鑑にTimeTreeリンクが未登録)。")
        return 0
    print(f"TimeTree 監視対象: {len(watch)} 件")
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    total = 0
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
        total += count
    print(f"TimeTree 取り込み完了: 合計 {total} 件")
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


def main() -> int:
    run_sites()
    run_timetree()
    if os.environ.get("ENABLE_X_INGEST") == "1":
        asyncio.run(run_x())
    return 0


if __name__ == "__main__":
    sys.exit(main())
