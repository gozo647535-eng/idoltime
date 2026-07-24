"""公式サイトからのイベント/チケット情報取得。

各アイドルの公式サイト(図鑑カタログの links に登録)を取得し、News/Schedule 等の
ページ本文を Gemini で解析して、今後のチケット/イベント情報を抽出する。
Xより壊れにくく、認証も不要。標準ライブラリ + bs4 + google-genai で動く。
"""
import gzip
import re
import urllib.request
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# News/Schedule/Ticket 系のサブページを見つけるためのキーワード
SUBPAGE_KEYWORDS = [
    "news", "schedule", "live", "ticket", "event", "information", "info",
    "スケジュール", "ニュース", "ライブ", "チケット", "イベント", "公演", "お知らせ",
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
    """News/Schedule 等の同一ドメイン内サブページURLを最大2件返す。"""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    found: List[str] = []
    for a in soup.find_all("a", href=True):
        blob = (a["href"] + " " + (a.get_text() or "")).lower()
        if any(k in blob for k in SUBPAGE_KEYWORDS):
            full = urljoin(base_url, a["href"])
            if urlparse(full).netloc == base_host and full not in found and full != base_url:
                found.append(full)
        if len(found) >= 2:
            break
    return found


class TicketType(BaseModel):
    name: str = Field(description="券種名 (例: 一般, VIP)")
    price: str = Field(description="料金 (例: 5000円)")


class SiteEvent(BaseModel):
    event_name: str = Field(description="イベント名/公演名")
    event_datetime: Optional[str] = Field(None, description="公演日時 (例: 2026/08/20 18:30)")
    sale_datetime: Optional[str] = Field(
        None,
        description="チケット発売/受付/先行の開始日時。分かる場合のみ 'YYYY-MM-DD HH:MM:SS'(年不明なら現在の年、秒は00)",
    )
    is_first_come: bool = Field(False, description="先着/一般発売(先着)なら True。抽選・先行(抽選)は False")
    ticket_url: Optional[str] = Field(None, description="チケット販売/詳細ページのURL")
    venue: Optional[str] = Field(None, description="会場")
    ticket_types: List[TicketType] = Field(default=[], description="券種と料金")


class SiteExtraction(BaseModel):
    events: List[SiteEvent] = Field(default=[], description="今後のチケット/イベント情報のリスト")


def extract_events(gemini_client, name: str, text: str) -> List[SiteEvent]:
    if not gemini_client or not text.strip():
        return []
    from google.genai import types

    prompt = f"""以下は「{name}」の公式サイトのテキストです。
今後開催されるライブ/イベントのうち、チケットの発売・受付・先行に関する情報が読み取れるものだけを、正確なJSONで抽出してください。
- すでに終了した公演、開催日が全く不明なものは含めない
- sale_datetime は分かる場合のみ 'YYYY-MM-DD HH:MM:SS' 形式で
- 情報が無ければ events は空配列

【公式サイト本文】
{text[:15000]}"""
    try:
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
        print(f"    Gemini抽出失敗: {e}")
        return []


def fetch_site_events(gemini_client, idol_slug: str, name: str, site_url: str):
    """公式サイト(+ News/Schedule サブページ)を取得し、(content_hash, [SiteEvent]) を返す。
    取得できなければ (None, [])。"""
    import hashlib

    home = _get(site_url)
    if not home:
        return None, []
    texts = [_html_to_text(home)]
    for sub in _find_subpages(home, site_url):
        h = _get(sub)
        if h:
            texts.append(_html_to_text(h))
    combined = "\n\n".join(texts)
    content_hash = hashlib.sha1(combined.encode("utf-8", "ignore")).hexdigest()
    events = extract_events(gemini_client, name, combined)
    return content_hash, events
