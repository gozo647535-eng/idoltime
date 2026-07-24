"""IdolLife 取り込みワーカー

図鑑に登録したアイドルの X アカウントを監視し、チケット投稿を Gemini(または
正規表現フォールバック)で解析して Supabase の auto_events テーブルへ書き込む。
元は Discord 通知ボット。解析ロジック(twikit 取得 / Gemini / Ticketdive スクレイプ /
日時正規化 / 先着判定)はそのまま流用し、出力先のみ Supabase に変更している。

GitHub Actions から定期実行される想定。既読位置(watermark)は Actions が揮発するため
Supabase の worker_state テーブルに保存する。
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import datetime
import re
import traceback
from typing import List, Optional
from pydantic import BaseModel, Field
from twikit import Client
import aiohttp
from bs4 import BeautifulSoup

import sb  # Supabase 入出力(標準ライブラリのみ)
from watchlist import build_watch_list  # 図鑑連動の監視リスト生成

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


x_client = Client("en-US")

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


def build_row(tweet, username: str, idol_slug: str, info: dict) -> dict:
    return {
        "tweet_id": str(tweet.id),
        "idol_slug": idol_slug,
        "account": username,
        "event_name": info.get("event_name"),
        "event_datetime": info.get("event_datetime"),
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


async def main() -> int:
    if not await login_x():
        print("X ログイン不可のため終了。")
        return 1
    # 図鑑(catalog.json)+ 有志追記 + 手動リストから監視対象を自動生成
    accounts = build_watch_list()
    if not accounts:
        print("監視対象がありません(図鑑にXリンクが未登録の可能性)。")
        return 0
    print(f"監視対象: {len(accounts)} アカウント")
    total = 0
    for entry in accounts:
        try:
            total += await check_account(entry["handle"], entry["idol_slug"])
        except Exception as e:
            print(f"{entry.get('handle')} の処理でエラー: {e}")
            traceback.print_exc()
    print(f"完了。処理した新規投稿: {total} 件。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
