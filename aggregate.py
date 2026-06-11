#!/usr/bin/env python3
"""
每日閱讀聚合器
- 讀 sources.json 裡的 RSS 來源
- 跟 data/seen.json 比對，挑出「上次跑之後的新文章」
- 對每篇新文章用 Claude 產生一句英文 summary + 中文標題/摘要
- 輸出成 public/index.html（最新一期）與 public/archive/YYYY-MM-DD.html（存檔）

設計重點：
- 第一次跑（seen.json 不存在）= 種子模式：把目前所有文章標記為已看，但不做摘要、不花 token。
  之後每天跑才會把「新增的」拿去摘要。避免第一次就燒一大筆。
- 沒有 ANTHROPIC_API_KEY 時仍可跑，只是用 feed 原本的摘要、不做中文翻譯。
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
from pathlib import Path

import feedparser

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.json"
SEEN_FILE = ROOT / "data" / "seen.json"
PUBLIC = ROOT / "public"
ARCHIVE = PUBLIC / "archive"

MODEL = "claude-haiku-4-5"          # 便宜、夠用；想更聰明可改 claude-sonnet-4-6
SEEN_RETENTION_DAYS = 60            # seen.json 只保留近 60 天，避免無限長大
MAX_ITEMS_PER_SOURCE = 15          # 每個來源每次最多處理幾篇新文（防爆量）
TZ = dt.timezone(dt.timedelta(hours=8))  # 台北時間，只用於顯示日期


# ---------------------------------------------------------------- 工具
def today_str() -> str:
    return dt.datetime.now(TZ).strftime("%Y-%m-%d")


def item_id(entry) -> str:
    """每篇文章的唯一識別。優先用 feed 給的 id/guid，否則用連結，再否則用標題雜湊。"""
    raw = getattr(entry, "id", None) or getattr(entry, "link", None) or getattr(entry, "title", "")
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def clean_text(s: str, limit: int = 600) -> str:
    """把 feed 摘要的 HTML 標籤拔掉，壓成純文字，截斷到 limit 字。"""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


# ---------------------------------------------------------------- Claude 摘要
def make_enricher():
    """回傳一個 enrich(title, summary) -> dict 的函式。沒有 key 就回傳 None（=不摘要）。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠️  沒有 ANTHROPIC_API_KEY，改用 feed 原摘要、不做中文翻譯。")
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    schema = {
        "type": "object",
        "properties": {
            "en_summary": {"type": "string", "description": "One concise English sentence on what the article is about."},
            "zh_title": {"type": "string", "description": "繁體中文標題翻譯"},
            "zh_summary": {"type": "string", "description": "一句繁體中文摘要，說明這篇在講什麼"},
        },
        "required": ["en_summary", "zh_title", "zh_summary"],
        "additionalProperties": False,
    }

    def enrich(title: str, summary: str) -> dict | None:
        prompt = (
            "You are summarizing an article for a bilingual (English + Traditional Chinese) "
            "reading digest. Based only on the title and feed excerpt below, return:\n"
            "- en_summary: one concise English sentence on the article's topic\n"
            "- zh_title: the title translated into Traditional Chinese\n"
            "- zh_summary: one Traditional-Chinese sentence on what it's about\n\n"
            f"Title: {title}\n"
            f"Excerpt: {summary or '(none)'}"
        )
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            text = next((b.text for b in resp.content if b.type == "text"), "")
            return json.loads(text)
        except Exception as e:  # noqa: BLE001 — 單篇失敗不該讓整批掛掉
            print(f"   ! 摘要失敗（{e.__class__.__name__}），這篇用原摘要。")
            return None

    return enrich


# ---------------------------------------------------------------- 主流程
def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict):
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=SEEN_RETENTION_DAYS)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(pruned, ensure_ascii=False, indent=0), encoding="utf-8")


def collect() -> tuple[list[dict], bool]:
    """回傳 (按來源分組的新文章, 是否為種子模式)。"""
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
    seen = load_seen()
    seed_mode = len(seen) == 0
    enrich = None if seed_mode else make_enricher()
    today = today_str()

    groups = []
    for src in sources:
        if not src.get("enabled", True):
            continue
        print(f"→ {src['name']}")
        feed = feedparser.parse(src["url"])
        new_entries = [e for e in feed.entries if item_id(e) not in seen]
        # 標記全部為已看（包含種子模式下的所有文章）
        for e in feed.entries:
            seen.setdefault(item_id(e), today)

        if seed_mode:
            print(f"   種子模式：記錄 {len(feed.entries)} 篇為已看，不摘要。")
            continue

        items = []
        for e in new_entries[:MAX_ITEMS_PER_SOURCE]:
            title = clean_text(getattr(e, "title", ""), 300)
            excerpt = clean_text(getattr(e, "summary", getattr(e, "description", "")))
            link = getattr(e, "link", "")
            enriched = enrich(title, excerpt) if enrich else None
            items.append({
                "en_title": title,
                "zh_title": (enriched or {}).get("zh_title", ""),
                "en_summary": (enriched or {}).get("en_summary", excerpt[:200]),
                "zh_summary": (enriched or {}).get("zh_summary", ""),
                "link": link,
            })
        if items:
            print(f"   新增 {len(items)} 篇。")
            groups.append({"name": src["name"], "items": items})

    save_seen(seen)
    return groups, seed_mode


# ---------------------------------------------------------------- HTML 輸出
PAGE_CSS = """
:root { --bg:#faf8f3; --card:#fff; --ink:#222; --sub:#666; --line:#e7e2d6; --accent:#b5532e; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,"PingFang TC","Helvetica Neue",Arial,sans-serif; line-height:1.55; }
.wrap { max-width:760px; margin:0 auto; padding:40px 20px 80px; }
header h1 { font-size:1.5rem; margin:0 0 4px; }
header .date { color:var(--sub); font-size:.95rem; }
.src { margin-top:36px; }
.src h2 { font-size:1.05rem; border-bottom:2px solid var(--accent); padding-bottom:6px;
  margin:0 0 14px; color:var(--accent); }
.item { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin-bottom:12px; }
.item a.t { text-decoration:none; color:var(--ink); }
.item a.t:hover .zh { text-decoration:underline; }
.item .zh { font-weight:600; font-size:1.05rem; }
.item .en { color:var(--sub); font-size:.9rem; margin-top:2px; }
.item .sum { margin-top:8px; font-size:.92rem; }
.item .sum .zh-s { color:var(--ink); }
.item .sum .en-s { color:var(--sub); font-style:italic; margin-top:2px; }
.empty { color:var(--sub); margin-top:30px; }
footer { margin-top:50px; padding-top:16px; border-top:1px solid var(--line);
  color:var(--sub); font-size:.85rem; }
footer a { color:var(--accent); }
"""


def esc(s: str) -> str:
    return html.escape(s or "")


def render_page(groups: list[dict], date: str, archive_links: list[str], seed_mode: bool) -> str:
    parts = [f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>晨間閱讀 · {date}</title><style>{PAGE_CSS}</style></head><body><div class="wrap">
<header><h1>📰 晨間閱讀</h1><div class="date">{date}</div></header>"""]

    if seed_mode:
        parts.append('<p class="empty">已完成初始化：記錄了目前各來源的文章為基準線。'
                     '從明天起，這裡會顯示「昨天之後新增」的內容。</p>')
    elif not groups:
        parts.append('<p class="empty">昨天沒有新內容。😌</p>')
    else:
        for g in groups:
            parts.append(f'<section class="src"><h2>{esc(g["name"])}</h2>')
            for it in g["items"]:
                zh_t = esc(it["zh_title"]) or esc(it["en_title"])
                en_t = esc(it["en_title"])
                parts.append(f'<div class="item"><a class="t" href="{esc(it["link"])}" target="_blank" rel="noopener">'
                             f'<div class="zh">{zh_t}</div>')
                if it["zh_title"]:
                    parts.append(f'<div class="en">{en_t}</div>')
                parts.append('</a><div class="sum">')
                if it["zh_summary"]:
                    parts.append(f'<div class="zh-s">{esc(it["zh_summary"])}</div>')
                if it["en_summary"]:
                    parts.append(f'<div class="en-s">{esc(it["en_summary"])}</div>')
                parts.append('</div></div>')
            parts.append('</section>')

    if archive_links:
        links = " · ".join(f'<a href="archive/{d}.html">{d}</a>' for d in archive_links[:30])
        parts.append(f'<footer>往期存檔：{links}</footer>')
    else:
        parts.append('<footer>由 reading_agg 自動產生。</footer>')

    parts.append('</div></body></html>')
    return "".join(parts)


def write_outputs(groups: list[dict], seed_mode: bool):
    date = today_str()
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    # 非種子模式才存當天的存檔
    if not seed_mode:
        (ARCHIVE / f"{date}.html").write_text(
            render_page(groups, date, [], seed_mode), encoding="utf-8")

    # 蒐集所有存檔日期（倒序）給首頁底部連結
    archive_dates = sorted(
        (p.stem for p in ARCHIVE.glob("*.html") if re.match(r"\d{4}-\d{2}-\d{2}", p.stem)),
        reverse=True,
    )
    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "index.html").write_text(
        render_page(groups, date, archive_dates, seed_mode), encoding="utf-8")
    print(f"✅ 已寫出 public/index.html（{date}）")


if __name__ == "__main__":
    groups, seed_mode = collect()
    write_outputs(groups, seed_mode)
