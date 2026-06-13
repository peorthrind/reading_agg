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
   │ 1. 讀 sources.json（來源清單 + 分類）       │
   │ 2. feedparser 抓每個 RSS                   │
   │ 3. 跟 data/seen.json 比對 → 找出新文章      │
   │ 4. 新文章逐篇丟 Haiku → 中英摘要            │
   │ 5. 全部新文丟 Sonnet → 開場白 + 今日精選     │
   │ 6. 產生 HTML（分類頁籤 + 精選 + 深色切換）    │
   │    public/index.html + archive/日期.html    │
   │ 7. 更新 data/seen.json                     │
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

### (d) 兩種模型，各司其職
```python
MODEL       = "claude-haiku-4-5"    # 逐篇摘要：高量、簡單 → 用便宜的
BRIEF_MODEL = "claude-sonnet-4-6"   # 每日精選/開場白：一天一次 → 用聰明的
```
- **為什麼分兩種**：逐篇摘要一天幾十~上百次，要便宜（Haiku $1/$5 per 1M），品質夠用。每日精選/開場白一天**只跑一次**、需要跨文章綜合判斷，值得用 Sonnet（$3/$15），多花約 2 美分/天。
- **原則**：依「呼叫頻率 × 任務難度」分配模型，不是全用最貴或全用最便宜。高頻簡單用小模型、低頻關鍵用大模型。

### (e) 沒有 API key 也能跑（graceful degradation）
`make_enricher()` 偵測不到 `ANTHROPIC_API_KEY` 時回傳 `None`，後續就用 feed 原摘要、跳過翻譯。
- **為什麼**：本機隨手測、或 key 暫時有問題時，程式不該整個掛掉。**降級而非崩潰**是好的工程習慣。

### (f) 量的上限
`MAX_ITEMS_PER_SOURCE = 75`：每個來源每次最多處理 75 篇新文。
- **為什麼**：預設**不做按內容/相關性的過濾**（使用方式是掃標題+摘要、想看才點，量不是問題）。上限純粹當「某 feed 突然灌 200 篇」的保險絲，設大一點避免正常高量站被誤砍。
- **截斷邏輯**：`new_entries[:N]` 取 feed 最前面 N 筆 = 保留最新的 N 篇；超過的會被標為已看、**永久跳過**（不是延到明天）。
- **為什麼是 75 而非 50**：分析 Economist `latest` feed（~300 篇 / 18 天）發現它每週四是印刷版整批上線的尖峰，單日 45→49→50 且還在往上頂，已逼近舊的 50 上限。因每天跑一次，平日新增量（10~20，週末個位數）遠低於上限，但週四若單日 >50，最舊那批會被靜默永久丟棄。拉到 75 給週四爆量留容錯邊際，平日成本為零。

### (f2) 來源層級過濾 `include_link_substr`（2026-06-13 加）
`sources.json` 每筆可選欄位。設了之後，該來源只保留**連結含此子字串**的文章，其餘丟棄。
- **用途**：砍掉同一 feed 裡混進來、URL 有規律的雜訊。例：36氪官方 `/feed` 把深度文（`36kr.com/p/`）和快訊（`36kr.com/newsflashes/`）混在一起，設 `"include_link_substr": "36kr.com/p/"` 就只留深度文。
- **執行順序（關鍵）**：過濾 **跑在 `[:MAX_ITEMS_PER_SOURCE]` 上限與 Haiku 摘要之前**——所以名額不會被雜訊吃掉、也不為雜訊花 token。
- **與「不做相關性過濾」原則的關係**：這是**按連結結構**的零成本硬篩，不靠 LLM、不判斷內容好壞，因此不違反 (f) 的設計取捨；它砍的是「整類 URL」而非「個別不夠精彩的文章」。
- **副作用**：被濾掉的文章在標記 seen 時**仍會進 seen.json**（標記發生在過濾之前，見 (a)），所以不會反覆出現，但也等於不會補回。

### (g) 分類頁籤
- `sources.json` 每筆有 `category`；`render_page()` 依 `CATEGORY_ORDER` 把來源分組成頁籤。
- 頁籤切換是**純前端 JS**（show/hide pane），一個 HTML 檔搞定，不需多頁面、不需後端。
- 未對到 `CATEGORY_ORDER` 的分類會排到最後、歸到「其他」——所以亂填 category 不會讓文章消失，只是分組位置不同。

### (h) 每日精選 + 開場白（generate_brief）
- 把當天**所有**新文編號後一次丟給 Sonnet，用 structured output 回 `{intro, top5:[{index, reason}]}`。
- **用 index 對應回文章**（而不是叫模型重打標題/連結）：模型只回編號，程式查表拿回正確的 title/link。**讓模型做判斷、程式做查表**，避免模型抄連結抄錯。
- 防呆：種子模式 / 沒新文 / 沒 key / 失敗 → 回 `None`，頁面就不顯示精選區塊，其餘照常。

### (i) 深色模式（純前端，零後端）
- 配色用 CSS 變數；`:root[data-theme="dark"]` 覆寫變數，切換只是改 `<html>` 上的 `data-theme`。
- **記憶**：選擇存 `localStorage`；沒選過就跟系統 `prefers-color-scheme` 走。
- **防閃爍**：`<head>` 裡有一段在畫面繪製前就先設好 `data-theme` 的小腳本（否則會先閃一下淺色再變深色）。
- 重點：這些都是**靜態頁面 + 瀏覽器端**就能做的事，不需要伺服器或資料庫——印證 §2「能用靜態檔解決就別架 server」。

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
| 加/移除來源、換來源的頁籤分類 | `sources.json`（`category` 欄） |
| 改頁籤有哪些、順序 | `aggregate.py` 的 `CATEGORY_ORDER` |
| 改逐篇摘要模型 | `aggregate.py` 的 `MODEL` |
| 改每日精選/開場白模型或提示詞 | `aggregate.py` 的 `BRIEF_MODEL` / `generate_brief()` |
| 改執行時間 | `digest.yml` 的 cron（記得用 UTC） |
| 改網頁長相、深色配色 | `aggregate.py` 的 `PAGE_CSS`（深色在 `[data-theme="dark"]`） |
| 改每來源處理上限/保留天數 | `aggregate.py` 頂部的常數 |

debug 時最快的方法：本機 `export ANTHROPIC_API_KEY=...` 然後直接 `python aggregate.py`，看 print 的逐來源訊息。
