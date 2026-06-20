# 規格：Inbox 處理與渲染（後半段，本 repo）

> 這份是 `reading_agg` 這邊要做的事：每天 7 點 Action 跑 `aggregate.py` 時，
> 除了照舊處理 RSS，再多處理 `inbox/` 裡那批「我手動丟進來的純文字」，
> 做**比新聞更詳細的長摘要 + 重點整理**，渲染進同一頁。
>
> **狀態：已實作（2026-06-20）。** `inbox/` 已有檔進來，後半段（`collect_inbox()` +
> 長摘要 + `render_long_item()` + 搬檔）已寫進 `aggregate.py`，本機以 stub 驗過解析與渲染。
> 真實 Sonnet 呼叫只會在 GitHub Action 跑（本機無 key）。前半段交接文件見 `docs/ingestion_handoff.md`。

---

## 1. 它在現有流程裡的位置

現有 `aggregate.py` 的主流程（見該檔）：
```
collect()  → 抓 RSS、跟 seen.json 比對、逐篇 Haiku 短摘要 → groups
generate_brief(groups) → Sonnet 產開場白 + 今日精選
write_outputs(groups, ...) → 渲染 public/index.html + archive/日期.html
```

要加的是一條**平行的來源**：讀 `inbox/`、做長摘要、併進 `groups`（或獨立區塊）一起渲染。

```
collect_rss()      → 照舊（就是現在的 collect()）
collect_inbox()    → 新增：讀 inbox/*.json → 每篇 Sonnet 長摘要 → 一個 group
groups = rss_groups + [inbox_group]
generate_brief(groups)   → 今日精選可一起納入（手動來源通常更值得進 Top）
write_outputs(...) → 多一個頁籤 / 區塊，且卡片版型更長（容得下重點整理）
處理完 → inbox/ 的檔搬到 processed/<日期>/ → commit
```

---

## 2. 🔑 INBOX 契約（兩邊唯一真實來源 — 改這裡要兩邊一起改）

> 這一段在 `docs/ingestion_handoff.md` 裡有**逐字相同的副本**。
> 任何欄位變動都要同步兩份文件，並 bump `schema_version`。
> 本段是「我們吃進來的東西長怎樣」；前半段保證每個 inbox 檔都符合這個格式。

### 檔名
```
inbox/<UTC日期>-<url的sha1前8碼>.json
例：inbox/2026-06-20-3f9a1c2b.json
```

### JSON 內容
```json
{
  "schema_version": 1,
  "id": "3f9a1c2b",
  "url": "https://www.example.com/some-article",
  "type": "article",
  "title": "文章或影片標題",
  "source": "站名 或 YouTube 頻道名",
  "full_text": "抽出來的正文 / 逐字稿純文字……",
  "lang": "en",
  "added_at": "2026-06-20T08:13:45Z",
  "added_by": "discord 顯示名稱（選填）",
  "note": "貼連結時附帶的一句話，沒有就空字串",
  "meta": {
    "word_count": 1820,
    "duration_seconds": null,
    "transcript_source": null
  }
}
```

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `schema_version` | int | ✅ | 目前固定 `1`。 |
| `id` | string | ✅ | `sha1(url)[:8]`，與檔名一致。去重用。 |
| `url` | string | ✅ | 原始連結（已去追蹤參數）。 |
| `type` | string | ✅ | `"article"` / `"youtube"`。 |
| `title` | string | ✅ | 標題。 |
| `source` | string | ✅ | 站名 / 頻道名。 |
| `full_text` | string | ✅ | 乾淨純文字全文 / 逐字稿。**我們摘要的唯一輸入。** |
| `lang` | string | ⬜ | ISO 639-1。 |
| `added_at` | string | ✅ | ISO 8601 UTC。 |
| `added_by` | string | ⬜ | 誰丟的。 |
| `note` | string | ⬜ | 附帶說明；**可餵進 prompt 當作摘要的側重提示**。 |
| `meta.word_count` | int | ⬜ | 字數，用於估 token / 排序 / 顯示。 |
| `meta.duration_seconds` | int\|null | ⬜ | 影片長度。 |
| `meta.transcript_source` | string\|null | ⬜ | `"captions"`/`"whisper"`/`null`。 |

### 我們這邊可以信任的前提
- inbox 裡的每個檔都是**完整、乾淨、可直接摘要**的全文（前半段保證不寫半成品）。
- bot 只新增、不修改 inbox 檔；**搬移/刪除是我們的責任**。

---

## 3. 要在 `aggregate.py` 加什麼

### 3.1 `collect_inbox() -> dict | None`
- glob `inbox/*.json`，逐檔讀進來、驗 `schema_version`。
- **跨天去重**：用一份 `data/inbox_seen.json`（記 `id`）擋掉已處理過的；
  正常情況檔已被搬走不會重讀，這層是保險。
- 對每篇呼叫 §4 的長摘要，組成 item，最後包成一個 group：
  ```python
  {"name": "我丟的", "category": "我丟的", "items": [...]}
  ```
  （`category` 名稱待定，見 §5。）
- 回傳該 group；inbox 空就回 `None`。

### 3.2 把它接進 `__main__`
```python
rss_groups, seed_mode = collect()          # 現有
inbox_group = None if seed_mode else collect_inbox()
groups = rss_groups + ([inbox_group] if inbox_group else [])
brief = None if seed_mode else generate_brief(groups)
write_outputs(groups, seed_mode, brief)
archive_processed_inbox()                   # 見 §6
```
> 種子模式（第一次跑）一樣跳過 inbox，維持「第一次不燒 token」的設計。

---

## 4. 長摘要 prompt（與 RSS 短摘要分開）

RSS 那條是 Haiku、吃 metadata、產一句。手動來源是**全文**，要 Sonnet、產結構化長摘要。

- **模型**：`claude-sonnet-4-6`（沿用 `BRIEF_MODEL`，或開新常數 `LONG_MODEL`）。
- **輸入**：`title` + `source` + `note`（若有，當側重提示）+ `full_text`。
- **輸入過長**：對 `full_text` 設上限（例如 ~40k token）；超過先截斷並在輸出註明
  「（基於前段內容）」，或日後做分段 map-reduce。先截斷即可。
- **建議 output schema**（json_schema，沿用現有 `output_config` 寫法）：
  ```json
  {
    "zh_title": "繁中標題",
    "one_liner": "一句話這篇在講什麼",
    "summary": "3–6 段繁中長摘要，講清楚論點與脈絡，不是逐段翻譯",
    "key_points": ["重點 1", "重點 2", "..."],
    "worth_reading": "一句：為什麼值得花時間讀全文 / 看全片"
  }
  ```
- 單篇失敗不該炸整批（比照現有 `enrich` 的 try/except）。

---

## 5. 渲染變更

手動來源的卡片資訊量比 RSS 大，版型要分開：

- **放哪**：新增一個頁籤 / 分類，例如 **「我丟的」** 或 **「精讀」**，排在最前面
  （加進 `CATEGORY_ORDER`）。或獨立成 brief 下方一個專區。先用頁籤最省事——
  現有 `buckets` / `order_categories` / tab JS 都能直接吃一個新 category。
- **卡片版型**：需要一個比 `render_item()` 更豐富的 `render_long_item()`：
  - 標題（連原文）+ 來源 +（影片時長 / 字數）
  - `one_liner`
  - `key_points` 做成 `<ul>`
  - `summary` 多段
  - `worth_reading` 一句收尾
  - 可加「📺 影片 / 📄 文章」小標示（看 `type`）
- CSS 沿用現有變數風格（`--card` / `--accent` …），加一個 `.longitem` class。
- **今日精選**：`generate_brief` 已吃所有 `groups`，手動來源會自然參與 Top5；
  可考慮在 prompt 裡略微偏好手動來源（它們是我主動挑的）。待定。

---

## 6. 處理完的狀態管理

- 渲染成功後，把這批 inbox 檔**搬到 `processed/<UTC日期>/`**（保留原檔，方便回溯 /
  重跑），而不是直接刪。
- 同步把 `id` 記進 `data/inbox_seen.json`（保留期比照 `seen.json`，例如 60–90 天）。
- `processed/` 與 `data/inbox_seen.json` 跟著當天的 commit 一起進 repo。
- ⚠️ **與攝取器的競態**：那邊（建議走 GitHub Contents API）只「新增」inbox 檔、
  我們只「讀 + 搬走我們開跑時看到的那批」。Action 開跑當下 glob 一次、只處理那批；
  跑到一半若有新檔進來，留到隔天。路徑各自獨立，不會壞。

---

## 7. 成本提醒

- RSS 短摘要：Haiku、metadata，便宜（現況一個月一兩鎂）。
- 手動長摘要：Sonnet、**全文輸入**，量級完全不同。一篇長文 ~幾千字、一支 90 分鐘
  影片逐字稿可能上萬字。一天幾篇還好，但要：
  - 對 `full_text` 設輸入上限（§4）
  - `meta.word_count` / `duration_seconds` 可拿來在 log 估量、或設每日上限保險絲

---

## 8. 對接時的 TODO（等前半段好了再做）

- [x] ~~建初始結構與 `.gitkeep`~~ → 改成 runtime 建立：`processed/<date>/` 在搬檔時 `mkdir`，
      `data/inbox_seen.json` 在第一次搬檔時寫出，`inbox/` 已存在。不需要 `.gitkeep`。
- [x] 實作 `collect_inbox()` + 長摘要 `make_long_enricher()/enrich_long()`
- [x] 實作 `render_long_item()` + 新頁籤「自選內容」（排第一）+ `.longitem`/`.longsec` CSS
- [x] 實作 `archive_processed_inbox()` + `inbox_seen` 維護（保留期比照 seen，60 天）
- [x] `generate_brief` 是否偏好手動來源 → **決定不特別偏好**：長 item 帶相容欄位（`zh_title`/
      `en_title`/`zh_summary`=one_liner），自然參與 Top5，prompt 不動。
- [x] 用真實 inbox 檔本機跑通 → 以 stub 驗過 `collect_inbox` 解析（4 檔）、長卡片渲染、
      頁籤順序、`archive` 搬檔 + `inbox_seen`。真實 Sonnet 呼叫待 Action 跑（本機無 key）。
- [ ] 更新 README 的「運作方式」與檔案說明，補上手動管道
- [x] GitHub Action 已能 commit `processed/` → workflow `git add` 增加 `inbox processed`，
      push 前加 `git pull --rebase`（攝取端 bot 也 push，避免非快轉衝突）。

---

## 9. 開發期間怎麼本機測（不依賴另一個 repo）

手刻一個符合 §2 契約的 JSON 丟進 `inbox/`，直接跑 `aggregate.py` 就能驗後半段，
完全不用等 bot：
```bash
mkdir -p inbox
cat > inbox/2026-06-20-deadbeef.json <<'JSON'
{ "schema_version":1, "id":"deadbeef", "url":"https://example.com/x",
  "type":"article", "title":"測試文章", "source":"Example",
  "full_text":"（貼一段夠長的真實文章內文進來測長摘要）",
  "lang":"zh", "added_at":"2026-06-20T00:00:00Z", "added_by":"me",
  "note":"", "meta":{"word_count":1200,"duration_seconds":null,"transcript_source":null} }
JSON
.venv/bin/python aggregate.py
open public/index.html
```
