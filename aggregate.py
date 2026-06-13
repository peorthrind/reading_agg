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

MODEL = "claude-haiku-4-5"          # 逐篇摘要用：便宜、夠用
BRIEF_MODEL = "claude-sonnet-4-6"  # 每日精選/開場白用：一天一次，用聰明點的
SEEN_RETENTION_DAYS = 60            # seen.json 只保留近 60 天，避免無限長大
MAX_ITEMS_PER_SOURCE = 75          # 每個來源每次最多處理幾篇新文（防爆量的保險絲）
TZ = dt.timezone(dt.timedelta(hours=8))  # 台北時間，只用於顯示日期

# 頁籤顯示順序。sources.json 裡沒對到這份清單的分類，會排在最後（歸到「其他」）。
CATEGORY_ORDER = ["科技 / AI", "新聞 / 時事", "長文 / 評論"]
DEFAULT_CATEGORY = "其他"


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


# ---------------------------------------------------------------- 每日精選 + 開場白
def generate_brief(groups: list[dict]) -> dict | None:
    """把當天所有新文丟給模型，產出 {intro, picks:[{title,link,source,reason}]}。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    flat = [{**it, "source": g["name"]} for g in groups for it in g["items"]]
    if not flat:
        return None

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    lines = []
    for i, it in enumerate(flat):
        t = it["zh_title"] or it["en_title"]
        s = it["zh_summary"] or it["en_summary"] or ""
        lines.append(f"[{i}] ({it['source']}) {t} — {s}")
    n_pick = min(5, len(flat))
    schema = {
        "type": "object",
        "properties": {
            "intro": {"type": "string", "description": "2–3 句繁體中文開場白，綜述今天的大局"},
            "top5": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "對應上面的文章編號"},
                        "reason": {"type": "string", "description": "繁中一句，為什麼值得讀"},
                    },
                    "required": ["index", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["intro", "top5"],
        "additionalProperties": False,
    }
    prompt = (
        f"以下是今天新增的 {len(flat)} 篇文章（編號 (來源) 標題 — 摘要）。請用繁體中文：\n"
        f"1. intro：寫 2–3 句『今天的大局』開場白，綜合最重要的脈絡，不要逐條流水帳。\n"
        f"2. top5：挑出最值得讀的 {n_pick} 篇（給編號），每篇一句說明為什麼值得讀。"
        f"偏好有觀點、有深度、跨主題的內容，盡量不要全挑同一個來源。\n\n"
        + "\n".join(lines)
    )
    try:
        resp = client.messages.create(
            model=BRIEF_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except Exception as e:  # noqa: BLE001 — 失敗就不顯示精選，不影響其餘
        print(f"   ! 今日精選產生失敗（{e.__class__.__name__}）")
        return None

    picks = []
    for p in data.get("top5", [])[:5]:
        i = p.get("index")
        if isinstance(i, int) and 0 <= i < len(flat):
            it = flat[i]
            picks.append({
                "title": it["zh_title"] or it["en_title"],
                "link": it["link"],
                "source": it["source"],
                "reason": p.get("reason", ""),
            })
    print(f"✅ 今日精選：開場白 + {len(picks)} 篇")
    return {"intro": data.get("intro", ""), "picks": picks}


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

        # 來源層級過濾：只保留連結含 include_link_substr 的文章。
        # 例：36氪 feed 把深度文(/p/)和快訊(/newsflashes/)混在一起，
        # 設成 "36kr.com/p/" 就只留深度長文、丟掉快訊。
        # 這一步刻意放在 per-source limit 與 Claude 摘要之前，避免名額被快訊吃掉、也不浪費 token。
        keep = src.get("include_link_substr")
        if keep:
            before = len(new_entries)
            new_entries = [e for e in new_entries if keep in getattr(e, "link", "")]
            print(f"   過濾 include_link_substr={keep!r}：{before} → {len(new_entries)} 篇")

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
            groups.append({
                "name": src["name"],
                "category": src.get("category", DEFAULT_CATEGORY),
                "items": items,
            })

    save_seen(seen)
    return groups, seed_mode


# ---------------------------------------------------------------- HTML 輸出
PAGE_CSS = """
:root { --bg:#faf8f3; --card:#fff; --ink:#222; --sub:#666; --line:#e7e2d6; --accent:#b5532e; }
:root[data-theme="dark"] { --bg:#1a1a1a; --card:#242424; --ink:#e8e8e8; --sub:#9a9a9a; --line:#383838; --accent:#e0764a; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,"PingFang TC","Helvetica Neue",Arial,sans-serif; line-height:1.55; }
.wrap { max-width:760px; margin:0 auto; padding:40px 20px 80px; }
header { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
header h1 { font-size:1.5rem; margin:0 0 4px; }
header .date { color:var(--sub); font-size:.95rem; }
#theme-toggle { background:none; border:1px solid var(--line); border-radius:8px;
  cursor:pointer; font-size:1.1rem; line-height:1; padding:8px 10px; color:var(--ink); flex:none; }
#theme-toggle:hover { border-color:var(--accent); }
.brief { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--accent);
  border-radius:10px; padding:16px 18px; margin-top:24px; }
.brief .intro { margin:0 0 12px; font-size:1rem; }
.brief h2 { font-size:.95rem; color:var(--accent); margin:0 0 8px; }
.brief ol.picks { margin:0; padding-left:1.2em; }
.brief ol.picks li { margin-bottom:10px; }
.brief ol.picks a { color:var(--ink); font-weight:600; text-decoration:none; }
.brief ol.picks a:hover { text-decoration:underline; }
.brief .psrc { color:var(--sub); font-size:.82rem; margin-left:6px; }
.brief .preason { color:var(--sub); font-size:.9rem; margin-top:2px; }
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
.tabs { display:flex; gap:8px; flex-wrap:wrap; margin-top:24px;
  border-bottom:1px solid var(--line); }
.tabs button { background:none; border:none; padding:10px 14px; cursor:pointer;
  font:inherit; color:var(--sub); border-bottom:2px solid transparent;
  margin-bottom:-1px; }
.tabs button:hover { color:var(--ink); }
.tabs button.active { color:var(--accent); border-bottom-color:var(--accent);
  font-weight:600; }
.tabs button .count { color:var(--sub); font-weight:400; font-size:.85em; }
.pane { display:none; }
.pane.active { display:block; }
footer { margin-top:50px; padding-top:16px; border-top:1px solid var(--line);
  color:var(--sub); font-size:.85rem; }
footer a { color:var(--accent); }
"""

TAB_JS = """
<script>
document.querySelectorAll('.tabs button').forEach(function(btn){
  btn.addEventListener('click', function(){
    var cat = btn.dataset.cat;
    document.querySelectorAll('.tabs button').forEach(function(b){
      b.classList.toggle('active', b===btn); });
    document.querySelectorAll('.pane').forEach(function(p){
      p.classList.toggle('active', p.dataset.cat===cat); });
  });
});
</script>
"""

THEME_JS = """
<script>
(function(){
  var tt=document.getElementById('theme-toggle');
  if(!tt)return;
  tt.addEventListener('click',function(){
    var next=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
    document.documentElement.setAttribute('data-theme',next);
    try{localStorage.setItem('theme',next);}catch(e){}
  });
})();
</script>
"""


def esc(s: str) -> str:
    return html.escape(s or "")


def render_item(it: dict) -> str:
    zh_t = esc(it["zh_title"]) or esc(it["en_title"])
    out = [f'<div class="item"><a class="t" href="{esc(it["link"])}" target="_blank" rel="noopener">'
           f'<div class="zh">{zh_t}</div>']
    if it["zh_title"]:
        out.append(f'<div class="en">{esc(it["en_title"])}</div>')
    out.append('</a><div class="sum">')
    if it["zh_summary"]:
        out.append(f'<div class="zh-s">{esc(it["zh_summary"])}</div>')
    if it["en_summary"]:
        out.append(f'<div class="en-s">{esc(it["en_summary"])}</div>')
    out.append('</div></div>')
    return "".join(out)


def render_source_section(g: dict) -> str:
    body = "".join(render_item(it) for it in g["items"])
    return f'<section class="src"><h2>{esc(g["name"])}</h2>{body}</section>'


def order_categories(buckets: dict) -> list[str]:
    """CATEGORY_ORDER 優先，未列出的分類接在後面，「其他」墊底。"""
    ordered = [c for c in CATEGORY_ORDER if c in buckets]
    ordered += [c for c in buckets if c not in CATEGORY_ORDER and c != DEFAULT_CATEGORY]
    if DEFAULT_CATEGORY in buckets:
        ordered.append(DEFAULT_CATEGORY)
    return ordered


def render_brief(brief: dict | None) -> str:
    if not brief or not brief.get("picks"):
        return ""
    out = ['<section class="brief">']
    if brief.get("intro"):
        out.append(f'<p class="intro">{esc(brief["intro"])}</p>')
    out.append('<h2>今日精選</h2><ol class="picks">')
    for p in brief["picks"]:
        out.append(f'<li><a href="{esc(p["link"])}" target="_blank" rel="noopener">{esc(p["title"])}</a>'
                   f'<span class="psrc">{esc(p["source"])}</span>'
                   f'<div class="preason">{esc(p["reason"])}</div></li>')
    out.append('</ol></section>')
    return "".join(out)


def render_page(groups: list[dict], date: str, archive_links: list[str],
                seed_mode: bool, brief: dict | None = None) -> str:
    parts = [f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>晨間閱讀 · {date}</title><style>{PAGE_CSS}</style>
<script>(function(){{try{{var t=localStorage.getItem('theme')||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}}})();</script>
</head><body><div class="wrap">
<header><div><h1>📰 晨間閱讀</h1><div class="date">{date}</div></div>
<button id="theme-toggle" aria-label="切換深淺色" title="切換深淺色">🌓</button></header>"""]

    parts.append(render_brief(brief))

    has_tabs = False
    if seed_mode:
        parts.append('<p class="empty">已完成初始化：記錄了目前各來源的文章為基準線。'
                     '從明天起，這裡會顯示「昨天之後新增」的內容。</p>')
    elif not groups:
        parts.append('<p class="empty">昨天沒有新內容。😌</p>')
    else:
        has_tabs = True
        buckets: dict[str, list[dict]] = {}
        for g in groups:
            buckets.setdefault(g["category"], []).append(g)
        ordered = order_categories(buckets)

        tabs = ['<div class="tabs">']
        for i, cat in enumerate(ordered):
            cnt = sum(len(g["items"]) for g in buckets[cat])
            cls = "active" if i == 0 else ""
            tabs.append(f'<button class="{cls}" data-cat="{esc(cat)}">'
                        f'{esc(cat)} <span class="count">({cnt})</span></button>')
        tabs.append('</div>')
        parts.append("".join(tabs))

        for i, cat in enumerate(ordered):
            cls = "pane active" if i == 0 else "pane"
            parts.append(f'<div class="{cls}" data-cat="{esc(cat)}">')
            parts.extend(render_source_section(g) for g in buckets[cat])
            parts.append('</div>')

    if archive_links:
        links = " · ".join(f'<a href="archive/{d}.html">{d}</a>' for d in archive_links[:30])
        parts.append(f'<footer>往期存檔：{links}</footer>')
    else:
        parts.append('<footer>由 reading_agg 自動產生。</footer>')

    parts.append('</div>')
    if has_tabs:
        parts.append(TAB_JS)
    parts.append(THEME_JS)
    parts.append('</body></html>')
    return "".join(parts)


def write_outputs(groups: list[dict], seed_mode: bool, brief: dict | None = None):
    date = today_str()
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    # 非種子模式才存當天的存檔
    if not seed_mode:
        (ARCHIVE / f"{date}.html").write_text(
            render_page(groups, date, [], seed_mode, brief), encoding="utf-8")

    # 蒐集所有存檔日期（倒序）給首頁底部連結
    archive_dates = sorted(
        (p.stem for p in ARCHIVE.glob("*.html") if re.match(r"\d{4}-\d{2}-\d{2}", p.stem)),
        reverse=True,
    )
    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC / "index.html").write_text(
        render_page(groups, date, archive_dates, seed_mode, brief), encoding="utf-8")
    print(f"✅ 已寫出 public/index.html（{date}）")


if __name__ == "__main__":
    groups, seed_mode = collect()
    brief = None if seed_mode else generate_brief(groups)
    write_outputs(groups, seed_mode, brief)
