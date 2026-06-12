# 待辦 / 可改進清單

依「價值 vs 成本」粗分。打勾的是已完成的基礎。

## ✅ 已完成（基礎版）
- [x] RSS 抓取 + 新文章比對（seen.json）
- [x] Claude 中英摘要
- [x] 靜態 HTML 輸出（中英對照、往期存檔）
- [x] GitHub Actions 每日排程 + Pages 部署
- [x] 種子模式、無 key 降級、單篇失敗容錯、量上限、seen 保留期
- [x] **分類頁籤**：3 類（科技/AI、新聞/時事、長文/評論），上方頁籤切換、各帶數量、純 JS。分類由 `sources.json` 的 `category` 欄決定。
- [x] **擴充科技/AI 來源**：TechCrunch、MIT Tech Review、MarkTechPost、INSIDE、36氪、Hacker News。
- [x] **每來源上限調為 75**：決定不做內容過濾（使用方式是掃標題+摘要、想看才點），上限只當「feed 暴衝」保險絲。原為 50，因 Economist 週四印刷版單日逼近 50（45→49→50），為留容錯邊際拉到 75。
- [x] **每日精選 + 開場白**：跨來源綜合，頂部一段「今天的大局」+ Top 5（含理由/連結），用 Sonnet 一天一次（~2 美分/天）。
- [x] **深色模式**：頂部按鈕切換，localStorage 記憶、跟系統預設走、防閃爍；純前端零成本。

---

## 內容覆蓋

- [x] **補上沒有 feed 的來源**：Politico Europe、Delayed Gratification 找到官方 feed 直接加；Foreign Affairs 無官方 feed，改用 **Google News RSS**（`site:` 查詢）。
- [ ] **Foreign Affairs 連結是 Google 轉址**（醜、偶有跳轉），標題帶「- Foreign Affairs」字尾。要更乾淨可改用自架 RSSHub，或在程式裡解析 Google News 連結還原成原始 URL + 去掉字尾。
- [ ] **Delayed Gratification feed 含非文章內容**（每週小測驗、audio），可加關鍵字/分類過濾。
- [ ] 其他沒 feed 的站，同樣用「先找隱藏 feed → 退 Google News RSS」的順序處理。RSSHub（需自架 server）留作要高品質時的選項。
- [ ] **付費 Substack 全文私人 feed**：目前抓的是公開 feed（摘要可能被截斷）。已訂閱的 Substack 可在帳號設定拿到「私人全文 RSS」，換上去摘要品質更好。注意：私人 feed URL 含密鑰，不能放進 public repo 的 `sources.json`——要改成從 Secret 讀。
- [ ] **更精準的「文章 vs 雜訊」過濾**：有些 feed（如 Economist 的 indicators、Quartz）會混進資料表、行情頁這類非文章內容，可加關鍵字/分類過濾。

## 摘要品質

- [ ] **摘要基於標題+feed 摘要，不是全文**——付費牆內文我們讀不到，所以摘要深度有限。這是設計取捨，但可在 B 桶來源用 `web_fetch` 抓公開的前幾段補強（注意 ToS 與禮貌）。
- [ ] **可選擇性升級到 Sonnet**：對重點來源用 Sonnet、其餘用 Haiku，平衡品質與成本（目前是全部 Haiku）。

## 閱讀體驗

- [x] **AI 精選/排序**：digest 頂部「今日精選 Top 5 + 開場白」，當日所有新文一次丟 Sonnet 排序。
- [x] **深色模式切換**：頂部按鈕、localStorage 記憶、跟系統走。
- [ ] **Email 每日推送**：除了網頁，每天寄一封 digest 到信箱（可用 GitHub Actions + SMTP/SendGrid，或 Cloudflare Email）。
- [ ] **「已讀」標記**：點過的條目用 localStorage 記、變灰，純前端。（先前提過的快贏，還沒做）
- [ ] **AI 自動分群（依主題、非依來源）**：目前頁籤是「依來源所屬分類」手動分。進一步可讓模型把每篇分到「地緣政治 / 科技 / 金融…」橫跨來源聚合（需要每篇多一次分類呼叫）。
- [ ] **「已讀」狀態 / 個人化**：目前是純展示。若想標記已讀，得引入輕量前端 + 儲存（會超出「靜態檔」的簡單性，要權衡）。

## 韌性 / 維運

- [ ] **失敗通知**：某個 feed 掛了或 workflow 失敗時通知自己（GitHub 預設會寄失敗 email，但可加更明確的）。
- [ ] **feed 健康檢查**：feed URL 會無聲改變或失效。可加一個定期檢查，feed 連不上或長期沒新文就提醒。
- [ ] **Pages 站台是公開網址**：目前任何人拿到網址都能看 digest（內容是公開文章 metadata，敏感度低）。若哪天想鎖站，需 Cloudflare Access 之類的方案或 GitHub Enterprise。
- [ ] **時區寫死台北**：`aggregate.py` 的 `TZ` 和 cron 的 UTC 換算各自獨立，改時間要記得兩邊一致。可考慮集中成一個設定。

## 程式碼層面

- [ ] **沒有自動化測試**：目前靠手動跑驗證。可加幾個單元測試（item_id 穩定性、seed 模式、HTML 跳脫）。
- [ ] **seen.json 會隨 repo 一起公開**：內容只是文章 ID 的 hash + 日期，無敏感資訊，但值得知道。
- [ ] **HTML 用字串拼接**：量大或要更複雜版型時，改用 Jinja2 之類的模板引擎會更好維護（目前刻意保持零模板依賴）。
