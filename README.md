# reading_agg — 每日閱讀聚合器

把一份你指定的 RSS 來源清單，每天早上彙整成一頁網頁：**依分類頁籤（科技/AI、新聞/時事、長文/評論）整理，每篇有標題、連結、一句中英對照摘要，頂部還有當天的「開場白 + 今日精選 Top 5」，並可切換深色模式**。
跑在 GitHub Actions（免費排程）+ GitHub Pages（免費網頁），不靠你的電腦開機、手機電腦都能開同一個網址。

只聚合各家**公開 RSS 的標題/摘要 metadata**，全文連回原站讀——不碰付費牆。

---

## 運作方式

```
每天 07:00 (台北) ── GitHub Actions 自動執行 aggregate.py
   │
   ├─ 讀 sources.json 的來源，抓各家 RSS
   ├─ 跟 data/seen.json 比對，挑出「上次之後新增」的文章
   ├─ 每篇新文章 → Claude (Haiku) 產生 英文 summary + 中文標題/摘要
   ├─ 讀 inbox/ 裡手動丟進來的全文 → Claude (Sonnet) 產生 結構化長摘要（見下）
   ├─ 當天所有新文（含自選內容）→ Claude (Sonnet) 產生 開場白 + 今日精選 Top 5
   ├─ 輸出 public/index.html（最新）+ public/archive/日期.html（存檔）
   ├─ 把處理過的 inbox 檔搬到 processed/<日期>/，id 記進 data/inbox_seen.json
   └─ commit 回 repo，並部署到 GitHub Pages
```

- **第一次執行 = 種子模式**：只記錄目前各來源文章為基準線，不摘要、不花 token（也跳過 inbox）。隔天起才會出現「新增」內容。
- **成本**：逐篇摘要用 Haiku（每天幾十篇 × ~400 tokens），每日精選用 Sonnet（一天一次，~2 美分）。合計一個月大約 **一兩美金**。模型可在 `aggregate.py` 的 `MODEL` / `BRIEF_MODEL` / `LONG_MODEL` 調整。

### 自選內容（手動丟連結 → 長摘要）

另一條平行管道：把想精讀的文章/影片連結，由攝取端（另一個 repo）煮成全文、寫成 `inbox/*.json`（格式契約見 `docs/ingestion_handoff.md`）。每天彙整時會：

- 讀 `inbox/`，每篇用 **Sonnet** 吃全文，產出結構化長摘要（一句話、重點清單、依原文約 1/5 長度的分段摘要、為何值得讀）。
- 渲染進**第一個頁籤「自選內容」**，用比 RSS 更豐富的長卡片（左側 accent 條、📺/📄 圖示、來源/時長）。
- `full_text` 設 4 萬字上限當成本保險絲（中文最壞 ≈ ≤40k token ≈ ≤$0.12 input/篇）；20 分鐘知識型影片實測單篇約 **$0.05**。
- 處理完把 inbox 檔搬到 `processed/<日期>/` 備查，靠 `data/inbox_seen.json` 跨天去重。

---

## 一次性設定（約 5 分鐘）

### 1. 建 GitHub repo 並推上去
```bash
cd /Users/netiberks/working/playground/reading_agg
git init && git add -A && git commit -m "init reading_agg"
gh repo create reading_agg --private --source=. --push   # 需要 gh CLI；或手動在 GitHub 建 repo 後 push
```

### 2. 加入 API key（Repo → Settings → Secrets and variables → Actions → New repository secret）
- Name: `ANTHROPIC_API_KEY`
- Value: 你的 key（`sk-ant-...`）

### 3. 開啟 GitHub Pages（Repo → Settings → Pages）
- Source 選 **GitHub Actions**

### 4. 跑第一次（Repo → Actions → "Daily reading digest" → Run workflow）
- 第一次是種子模式，網頁會顯示「已完成初始化」。
- 之後每天早上自動跑；想立刻看效果可隔幾分鐘再手動觸發一次（但要有新文章才會出現內容）。

網址會是：`https://<你的帳號>.github.io/reading_agg/`

---

## 改來源清單

編輯 `sources.json`，加一筆：
```json
{ "name": "顯示名稱", "url": "https://....../feed", "enabled": true }
```
`enabled: false` 會跳過。改完 push 即可，下次執行生效。

---

## 本機測試（選用，確認中文摘要有跑出來）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

.venv/bin/python aggregate.py          # 第一次：種子模式

# 模擬有新文章：砍掉幾筆 seen 再跑，這次會真的呼叫 Claude
.venv/bin/python - <<'PY'
import json, pathlib
p = pathlib.Path("data/seen.json"); d = json.loads(p.read_text())
for k in list(d)[:8]: del d[k]
p.write_text(json.dumps(d, ensure_ascii=False))
PY
.venv/bin/python aggregate.py          # 第二次：產生中英對照摘要

open public/index.html                  # 打開看結果
```

---

## 檔案說明

| 檔案 | 作用 |
|---|---|
| `sources.json` | 你的來源清單（會編輯的就這個） |
| `aggregate.py` | 主程式：抓 feed → 比對新文 → Claude 摘要 → 產生 HTML |
| `.github/workflows/digest.yml` | 排程與部署（cron 時間在這裡改） |
| `data/seen.json` | 已看過的文章記錄（自動產生/維護，保留近 60 天） |
| `inbox/` | 攝取端丟進來、待長摘要的全文 `*.json`（格式契約見 `docs/ingestion_handoff.md`） |
| `processed/<日期>/` | 已長摘要、搬離 inbox 的檔（備查，自動產生） |
| `data/inbox_seen.json` | 自選內容跨天去重記錄（自動產生/維護，保留近 60 天） |
| `public/` | 產出的網頁（自動產生，由 Pages 部署） |

---

## 之後可以加的東西

- **Foreign Affairs / 其他沒 feed 的站**：架一個 RSSHub 幫它們生 feed，URL 填進 `sources.json`。
- **Email 每日推送**：除了網頁，每天寄一封 digest 到信箱。
- **「已讀」標記**：點過的條目變灰（純前端 localStorage）。
- **Email 推送**：除了網頁，每天寄一封 digest 到信箱。

要做哪個再跟我說。
