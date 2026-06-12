# 技術細節（寫給 junior engineer）

這份說明你會看到「用了什麼、為什麼這樣選、有什麼坑」。目標是你讀完能自己改、自己 debug。

---

## 1. 整體架構：一條每天跑一次的生產線

```
GitHub Actions (cron, 每天 23:00 UTC)
        │
        ▼
  python aggregate.py
        │
   ┌────┴─────────────────────────────────────┐
   │ 1. 讀 sources.json（來源清單）             │
   │ 2. feedparser 抓每個 RSS                   │
   │ 3. 跟 data/seen.json 比對 → 找出新文章      │
   │ 4. 新文章丟給 Claude → 中英摘要             │
   │ 5. 產生 public/index.html + archive/日期.html│
   │ 6. 更新 data/seen.json                     │
   └────┬─────────────────────────────────────┘
        │
   git commit data/ + public/ 回 repo
        │
   upload-pages-artifact → deploy-pages
        │
        ▼
  https://<帳號>.github.io/reading_agg/
```

核心觀念：**這是一個 stateless 的批次工作 + 一份持久化的狀態檔（seen.json）**。每次執行都是「載入狀態 → 算差異 → 寫回狀態 + 產出」。沒有長駐 server、沒有資料庫。

---

## 2. 為什麼選這個技術組合

### GitHub Actions 當排程器
- **為什麼**：免費的 cron，不需要自己養一台一直開機的機器。它本質上是「每天租一台 ubuntu 跑幾分鐘然後關掉」。
- **替代方案**：自己 Mac 的 launchd（電腦沒開就不跑）、VPS（要錢要顧）。對「每天跑一次的輕量工作」，Actions 是甜蜜點。

### GitHub Pages 當網頁主機
- **為什麼**：免費靜態網站主機，跟 repo 綁在一起。我們的產出就是純 HTML，不需要後端。
- **限制（踩過的坑）**：免費方案只能對 **public repo** 開 Pages；而且 Pages 站台一律是公開網址（除非 Enterprise）。

### Python + feedparser + anthropic
- **feedparser**：解析 RSS/Atom 的事實標準函式庫。各家 feed 格式有差異（RSS 2.0、Atom、RDF），它幫你抹平，統一用 `entry.title` / `entry.link` / `entry.summary` 存取。自己用 regex 解 XML 是地獄，別做。
- **anthropic**：官方 SDK，呼叫 Claude API。
- **為什麼是 Python**：這類「抓資料、轉換、輸出」的膠水活，Python 生態最順。

### 為什麼產「靜態 HTML」而不是做一個 web app
- 需求是「每天看一頁昨天的東西」，內容一天才變一次。沒有互動、沒有使用者輸入。
- 靜態檔最簡單、最便宜、最不會壞。沒有 server 要維護、沒有 runtime 要擔心。
- **原則**：能用靜態檔解決的，不要架 server。

---

## 3. 關鍵設計決策逐一說明

### (a) 怎麼判斷「新文章」— seen.json
每篇文章算一個唯一 ID：
```python
def item_id(entry):
    raw = entry.id or entry.link or entry.title   # 優先用 feed 的 guid
    return hashlib.sha1(raw.encode()).hexdigest()
```
`data/seen.json` 是 `{ 文章ID: 第一次看到的日期 }`。每次執行：feed 裡 ID 不在 seen 的 = 新文章。

- **為什麼用 ID 比對，而不是「抓最近 24 小時」**：時間窗的做法在「某天 workflow 失敗沒跑」時會漏文章，或重複顯示。用 seen 記錄就天然冪等（idempotent）——跑幾次結果都對。
- **為什麼 ID 要 hash**：feed 的 guid/link 可能很長、含奇怪字元，hash 成固定長度當 key 乾淨。
- **防無限長大**：`save_seen()` 只保留近 60 天的記錄（`SEEN_RETENTION_DAYS`）。文章不會在 feed 裡待超過這麼久，舊記錄可以丟。

### (b) 種子模式（seed mode）
第一次跑時 seen.json 不存在 → 把目前所有文章標記為已看，**但不摘要、不花 token**。
- **為什麼**：如果第一次就把每個 feed 裡現存的幾十~幾百篇全當「新文章」拿去摘要，會瞬間燒一筆錢、產出一頁雜訊。種子模式先立「基準線」，隔天起才報「真正新增」的。
- 程式判斷：`seed_mode = len(seen) == 0`。

### (c) 中英摘要 — 用 structured output 而非「叫模型回 JSON 然後祈禱」
```python
output_config={"format": {"type": "json_schema", "schema": SCHEMA}}
```
- **為什麼**：我們要的是 `{en_summary, zh_title, zh_summary}` 三個欄位。用 structured output 把回應**約束成符合 schema 的 JSON**，API 層保證格式，省去「模型多嘴一句『以下是摘要：』害 json.loads 爆掉」的麻煩。
- **單篇失敗不拖垮整批**：每篇摘要包在 try/except，失敗就用 feed 原摘要 fallback，繼續跑下一篇。批次工作的原則——**一顆壞掉的蛋不該毀掉整盤**。

### (d) 模型選 Haiku
```python
MODEL = "claude-haiku-4-5"
```
- **為什麼**：摘要+翻譯是相對簡單、高量的任務，Haiku 便宜（$1/$5 per 1M tokens）又快，品質夠。估算月費幾毛~一美金。
- 想更聰明（更精準的摘要）就改成 `claude-sonnet-4-6`，貴一點但仍便宜。這是一行就能調的旋鈕。

### (e) 沒有 API key 也能跑（graceful degradation）
`make_enricher()` 偵測不到 `ANTHROPIC_API_KEY` 時回傳 `None`，後續就用 feed 原摘要、跳過翻譯。
- **為什麼**：本機隨手測、或 key 暫時有問題時，程式不該整個掛掉。**降級而非崩潰**是好的工程習慣。

### (f) 量的上限
`MAX_ITEMS_PER_SOURCE = 15`：每個來源每次最多處理 15 篇新文。
- **為什麼**：防爆量。萬一某來源一天灌了一堆，不會失控地呼叫 API。

---

## 4. GitHub Actions workflow 的重點（.github/workflows/digest.yml）

### 觸發
```yaml
on:
  schedule:
    - cron: "0 23 * * *"   # UTC！23:00 UTC = 隔天 07:00 台北
  workflow_dispatch: {}    # 允許手動觸發（測試用）
```
- **坑**：cron 是 **UTC 時間**，不是你的本地時間。台北 = UTC+8，要自己換算。
- **另一個 YAML 坑**：`on:` 這個 key 在 YAML 會被某些解析器當成布林 `True`（YAML 1.1 把 on/off/yes/no 視為布林）。GitHub 自己會正確處理，但你用 PyYAML 驗證時 `d['on']` 會 KeyError，要查 `d[True]`。不是 bug。

### 權限
```yaml
permissions:
  contents: write   # 才能把 seen.json/public commit 回 repo
  pages: write       # 部署 Pages
  id-token: write    # Pages 部署的 OIDC 驗證需要
```
- **原則**：workflow 預設權限很小，要什麼明確開什麼（最小權限）。

### 為什麼要 commit 回 repo
seen.json 是狀態，必須跨次執行保存——所以每次跑完 commit 回去。public/ 也 commit 回去，這樣 archive 存檔會累積（部署時上傳的是累積後的整個 public/，歷史不會掉）。

### concurrency
```yaml
concurrency:
  group: digest
  cancel-in-progress: false
```
- **為什麼**：避免兩份同時跑（例如手動觸發撞到排程），兩邊都 push 會衝突。

---

## 5. 安全性：API key 怎麼處理

- key **不寫進程式碼、不進 repo**，只放 GitHub **Repository Secret**。
- workflow 裡用 `${{ secrets.ANTHROPIC_API_KEY }}` 注入成環境變數，程式用 `os.environ.get(...)` 讀。
- **原則**：機密永遠走環境變數/secret store，永遠不進版本控制。這也是為什麼 repo 改 public 不會洩漏 key。

---

## 6. 如果你要改這個專案，從哪下手

| 想做的事 | 改哪裡 |
|---|---|
| 加/移除來源 | `sources.json` |
| 改摘要模型/品質 | `aggregate.py` 的 `MODEL` |
| 改執行時間 | `digest.yml` 的 cron（記得用 UTC） |
| 改網頁長相 | `aggregate.py` 的 `PAGE_CSS` 和 `render_page()` |
| 改每來源處理上限/保留天數 | `aggregate.py` 頂部的常數 |

debug 時最快的方法：本機 `export ANTHROPIC_API_KEY=...` 然後直接 `python aggregate.py`，看 print 的逐來源訊息。
