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
import lineup  # 出演欄から図鑑の登録グループを突合
import timetree  # TimeTree 公開カレンダーからの取得エンジン
import ticketdive  # TicketDive イベントページの構造化パーサ(チケット情報の高精度化)
import ticketsite  # LivePocket / TiGET イベントページの構造化パーサ
import yahoo_rt  # Yahoo!リアルタイム検索からのチケットサイト・イベント発見
from watchlist import (
    build_idol_name_list,
    build_site_watch_list,
    build_timetree_watch_list,
    build_watch_list,
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


def build_site_row(slug: str, name: str, site_url: str, ev) -> Optional[dict]:
    """公式サイト由来の SiteEvent を auto_events 行に変換。tweet_id は合成した安定IDで重複排除。"""
    if not ev.event_name:
        return None
    if _RECEPTION_ONLY.search(ev.event_name) and not _REAL_GATHERING.search(ev.event_name):
        print(f"  チケット受付のためイベントにしません: {ev.event_name[:40]}")
        return None
    sale = None
    if ev.sale_datetime:
        sale = to_timestamptz(normalize_datetime_string(ev.sale_datetime) or ev.sale_datetime)
    event_dt = normalize_event_datetime(ev.event_datetime)
    sale = _sane_sale_datetime(sale, event_dt, ev.event_name)
    key = hashlib.sha1(
        f"{slug}|{ev.event_name}|{ev.event_datetime}|{ev.sale_datetime}".encode("utf-8")
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
        "tweet_url": site_url,
    }


def _site_event_dedup_key(name, datetime_str):
    """同一公演の重複判定キー(正規化した名前 + 日付部分)。"""
    n = re.sub(r"\s+", "", (name or "").lower())
    d = (normalize_event_datetime(datetime_str) or "")[:10]
    return (n, d)


def merge_site_events(jsonld_events, gemini_events):
    """JSON-LD(構造化データ・高精度)を優先し、Geminiが拾った未カバーの公演を追加する。"""
    merged = []
    seen = set()
    for ev in list(jsonld_events) + list(gemini_events):
        if not getattr(ev, "event_name", None):
            continue
        k = _site_event_dedup_key(ev.event_name, ev.event_datetime)
        if k in seen:
            continue
        seen.add(k)
        merged.append(ev)
    return merged


def run_sites() -> int:
    """公式サイトを巡回してイベント/チケット情報を取り込む(主軸)。
    JSON-LD(schema.org Event)を Gemini不要で抽出し、Gemini解析結果と統合する。"""
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
            content_hash, combined, jsonld_events = sites.fetch_site_content(url)
        except Exception as e:
            print(f"  失敗: {e}")
            continue
        if content_hash is None:
            continue
        state_key = f"site:{slug}"
        # 本文/構造化データに変化が無ければ LLM を呼ばずにスキップ(無料枠の節約)。
        # ただし「本文に今後の日付があるのに抽出0件」だった回は <hash>|z1 を記録してあり、
        # 内容が同じでも一度だけ取り直す(取りこぼしがそのまま固定化するのを防ぐ)。
        seen = sb.get_last_seen(state_key)
        if seen == content_hash:
            print("  変更なし。スキップ")
            continue
        retry_zero = seen == f"{content_hash}|z1"
        if retry_zero:
            print("  前回0件だったため再抽出します")
        llm_events = sites.extract_events_any(groq_client, gemini_client, name, combined)
        llm_ok = llm_events is not None
        if not llm_ok:
            llm_events = []
        if not llm_ok and not jsonld_events:
            # LLMが一時失敗(枠切れ等)、かつJSON-LDも無し → 状態を更新せず次回再試行
            print("  抽出できず(次回再試行)")
            continue

        merged = merge_site_events(jsonld_events, llm_events)
        count = 0
        emitted_ids = set()
        for ev in merged:
            row = build_site_row(slug, name, url, ev)
            if row:
                enrich_row_with_ticketdive(row)
                try:
                    sb.upsert_auto_event(row)
                    emitted_ids.add(row["tweet_id"])
                    count += 1
                except Exception as e:
                    print(f"  書き込み失敗: {e}")

        if llm_ok:
            # 再突合: 今回出力しなかった過去のsite行(終了・削除・変更済み)を掃除して最新に保つ
            removed = 0
            try:
                for old_id in sb.get_auto_event_ids(slug, prefix=f"site:{slug}:"):
                    if old_id not in emitted_ids:
                        sb.delete_auto_event(old_id)
                        removed += 1
            except Exception as e:
                print(f"  再突合(削除)でエラー: {e}")
            if count == 0 and not retry_zero and sites.has_future_dates(combined):
                # 素材に今後の日付があるのに1件も取れていない → 次回もう一度だけ試す
                sb.set_last_seen(state_key, f"{content_hash}|z1")
                print("  0 件(本文に今後の日付あり)。次回もう一度抽出します")
            else:
                sb.set_last_seen(state_key, content_hash)
                print(f"  {count} 件反映 / {removed} 件削除")
        else:
            # LLM一時失敗: JSON-LD分だけ即反映。削除・状態更新はせず次回再試行する
            # (既存のLLM由来イベントを枠切れで消さないため、reconcileとhash更新は行わない)
            print(f"  {count} 件反映(JSON-LDのみ / LLM次回再試行)")
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
        "event_datetime": normalize_event_datetime(ev.get("event_datetime")),
        "open_time": open_t,
        "entry_time": entry_t,
        "sale_datetime": sale,
        "is_first_come": bool(ev.get("is_first_come")),
        "ticket_url": ev.get("ticket_url"),
        "ticket_types": [],
        "max_tickets": None,
        "other_details": clean_venue(ev.get("venue")),
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
    if not date or date < today or not row.get("tweet_id", "").startswith("tt:"):
        return 0
    text = f"{row.get('event_name') or ''}\n{row.get('raw_text') or ''}"
    added = 0
    for disp, other in lineup.names_in_text(text, lineup_idx):
        if other == row["idol_slug"]:
            continue
        if added >= MAX_LINEUP_FANOUT:
            print(f"    出演者の相互反映が{MAX_LINEUP_FANOUT}件を超えたため打ち切りました")
            break
        xr = dict(row)
        xr["idol_slug"] = other
        xr["tweet_id"] = f"ttx:{other}:{row['tweet_id'][3:]}:{date.replace('-', '')}"
        try:
            sb.upsert_auto_event(xr)
            touched.add(other)
            added += 1
        except Exception as e:
            print(f"    相互反映の書き込み失敗({disp}): {e}")
    return added


def run_timetree() -> int:
    """TimeTree 公開カレンダーを巡回してイベント/チケット情報を取り込む。"""
    watch = build_timetree_watch_list()
    if not watch:
        print("TimeTree の監視対象がありません(図鑑にTimeTreeリンクが未登録)。")
        return 0
    print(f"TimeTree 監視対象: {len(watch)} 件")
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    lineup_idx = lineup.build_index(build_idol_name_list())
    print(f"出演者の相互反映に使う登録グループ: {len(lineup_idx)} 件")
    total = 0
    fanout = 0
    fanout_slugs = set()
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
        total += count
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
GROUP_QUERY_BATCH = int(os.environ.get("GROUP_QUERY_BATCH", "45"))


def _group_search_queries(today: str) -> List[str]:
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
        covered = sb.get_covered_slugs(today)
    except Exception as e:
        print(f"  カバー状況の取得に失敗(全グループを同等に扱います): {e}")
        covered = set()

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
        ranked.append((1 if slug in covered else 0, last, slug, q))
    ranked.sort(key=lambda t: (t[0], t[1]))
    picked = ranked[:GROUP_QUERY_BATCH]
    try:
        sb.set_last_seen_many([(f"yq:{slug}", today) for _, _, slug, _q in picked])
    except Exception as e:
        print(f"  検索日の記録に失敗(次回も同じ順番になります): {e}")
    if picked:
        uncovered = sum(1 for p in picked if p[0] == 0)
        print(f"グループ名検索: {len(picked)} 件 "
              f"(出演情報が無いグループ {uncovered} 件を優先 / 候補 {len(ranked)} 件)")
    return [q for _, _, _, q in picked]


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
    return {
        "tweet_id": f"yrt:{slug}:{_event_key(source, url)}:{dt.replace('-', '')}",
        "idol_slug": slug,
        "account": None,
        "event_name": info.get("event_name"),
        "event_datetime": dt if multi_day else normalize_event_datetime(info.get("event_datetime")),
        "open_time": info.get("open_time"),
        "entry_time": None,
        "sale_datetime": to_timestamptz(info.get("sale_datetime")),
        "is_first_come": bool(info.get("is_first_come")),
        "ticket_url": url,
        "ticket_types": info.get("ticket_types") or [],
        "max_tickets": None,
        "other_details": clean_venue(info.get("venue")),
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


def run_yahoo_rt() -> int:
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
    queries = list(YRT_QUERIES) + _group_search_queries(today)
    # クエリ数が増えるため、サイトごとの上限も引き上げる(当日取得済みURLは
    # キャッシュで省かれるので、2回目以降の取得コストは増えない)。
    by_site = yahoo_rt.collect_event_urls(queries, limit_per_site=150)
    total_urls = sum(len(v) for v in by_site.values())
    if not total_urls:
        print("Yahooリアルタイム: チケットサイトのリンクは見つかりませんでした。")
        return 0
    print("Yahooリアルタイム: " + " / ".join(f"{k} {len(v)}件" for k, v in by_site.items()))

    total = 0
    touched = set()
    skipped = 0
    for source, urls in by_site.items():
        for url in urls:
            # 同じイベントは何度も出回るので、当日すでに取り込んだURLは再取得しない
            # (高頻度実行でも相手サイトへの負荷と実行時間を増やさないため)
            cache_key = _yrt_cache_key(source, url)
            try:
                if sb.get_last_seen(cache_key) == today:
                    skipped += 1
                    continue
            except Exception:
                pass
            info = _td_info(url) if source == "ticketdive" else ticketsite.fetch_event(url)
            if not info:
                continue
            try:
                sb.set_last_seen(cache_key, today)
            except Exception:
                pass
            for idol in idols:
                name, slug = idol["name"], idol["idol_slug"]
                dates, multi_day = _match_dates(source, info, name, today)
                for dt in dates:
                    if not dt or dt < today:
                        continue  # 終了済みの公演は取り込まない
                    try:
                        sb.upsert_auto_event(_build_yrt_row(slug, source, url, info, dt, multi_day))
                        touched.add(slug)
                        total += 1
                        print(f"  → [{source}] {name}: {info.get('event_name')} ({dt})")
                    except Exception as e:
                        print(f"  書き込み失敗: {e}")

    # 発見は機会依存(今日の検索に出ない=中止ではない)ので再突合削除はせず、
    # 開催日が過ぎた行だけを掃除する。
    removed = sum(_cleanup_past_yrt(slug, today) for slug in touched)
    print(f"Yahooリアルタイム取り込み完了: 合計 {total} 件 / "
          f"当日取得済みスキップ {skipped} 件 / 過去分 {removed} 件削除")
    return total


def main() -> int:
    # X_ONLY=true は twikit(X取り込み)の動作確認用。サイト巡回/TimeTreeを飛ばしXだけ実行する。
    x_only = os.environ.get("X_ONLY", "").lower() == "true"
    # DISCOVERY_ONLY=true はチケットサイト発見だけを回す軽量モード(数分)。
    # 重いサイト巡回(Groqのレート順守で約30分)を挟まずに済むので、高頻度で実行できる。
    discovery_only = os.environ.get("DISCOVERY_ONLY", "").lower() == "true"
    if discovery_only:
        run_yahoo_rt()
        return 0
    if not x_only:
        run_sites()
        run_timetree()
        run_yahoo_rt()
    if x_only or os.environ.get("ENABLE_X_INGEST") == "1":
        asyncio.run(run_x())
    return 0


if __name__ == "__main__":
    sys.exit(main())
