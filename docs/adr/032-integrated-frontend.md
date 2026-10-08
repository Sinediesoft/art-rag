# ADR 032：整合版前端——入口＋工廠／藝術模組，對話在左、成果在右

- 日期：2026-10-08
- 狀態：採用（`agent/task-20261008-001-replace-frontend-with-demo`，待 codex-review／codex-structure 審查）
- 取代：ADR 016 的版面（「首頁就是智慧助理、功能頁從『深入』按鈕進去」）。ADR 016 的分派規則（`deepActions()`、
  看不到的圖紙不給按鈕）保留，搬到 `frontend/src/shell/deep.ts`。
- 介面來源：工作根目錄的 `ArtRAG-前端demo/`（整合版：入口 01 Quiet、工廠模組 10 Night、藝術模組暖色）。
  只移植原始碼架構與 CSS；`source/src/mock/`、打包好的 `assets/app.js`、`assets/models.js` 與示範回覆一律不用。
- 不改後端：API、SSE、`shared/agent.yaml`、七段權限控管（ADR 015、030）都不變。

## 決定

1. **三個畫面**：`/` 入口（問候、建議問句、輸入框，左側可收合的對話紀錄；手機是抽屜）、`/c/<id>` 入口裡的一段對話、
   `/factory/<id>`、`/art/<id>` 模組（左對話、右展示區，分隔線 30%–70%、雙擊回到一半；窄螢幕改成「對話／展示區」分頁）。
   原本的功能頁網址（`/search`、`/artworks/*`、`/drawings/*`、`/reconstruct`、`/inventory`、`/schedule`、`/approvals`、
   `/compare`、`/photo-diff`、`/batch`、`/compare-items`、`/admin`）照舊直接開啟，套所屬模組的外觀（`views/FeatureView.tsx`）。
2. **入口只回簡介**：七段流程分派到工廠（圖紙、問答、庫存、3D、排程、修改）或藝術（以文搜畫、畫作問答）時，
   簡介下方出現「進入工廠模組／進入藝術模組」。被擋下、降級（第 1 段或第 6 段「查無資料」）、要你選、超出範圍、閒聊、
   出錯都不給跳轉（`shell/outputs.ts` 的 `domainOf`）。
3. **成果全部來自真實回應**（`shell/outputs.ts` 的 `deriveOutputs`）：
   | 成果 | 來源 |
   |---|---|
   | 圖紙 | 第 1 段確認看得到的那一張（`auth.filter.doc_level` 不是 null）或圖紙查找第 1 名；展示區讀 `/parts/{id}` |
   | 3D | `/cad/reconstruct` 的 `model.stl`（按「開始轉換」才執行） |
   | 庫存／查詢 | `/inventory/ask` 的結果表 |
   | 排程 | 目前排程 `/production/overview`；按「開始排程」後 `/schedule/solve` 的解 |
   | 作品、細看 | `/artworks/{id}`（大圖、段落）＋`/artworks/{id}/colors`（主色、色塊分布圖） |
   | 年表 | `/artworks` 依年代排列（同一位畫家與 ±40 年的館藏）＋「創作背景」類段落 |
   | 相似作品 | `/search/text`（使用者的搜尋句，或作品的風格標籤＋媒材）；並排比較 `/compare/items` |
4. **對話在全域 store 執行**（`shell/store.tsx`、`shell/runner.ts`）：換畫面、到功能頁再回來不會中斷或重送；
   停止＝abort 這一輪的請求與串流；重新產生＝同一句重跑第 1～7 段。模組裡說「這張圖／這幅畫」時，把正在看的對象
   帶進問句（`〈連接法蘭〉把這張圖轉成 3D`）：`/agent/route` 一句一句判斷、沒有上下文，帶進去的名稱一樣經過第 1 段授權。
5. **七段軌跡**（`shell/stages.ts`）：第 1、2 段依 `/agent/route`；第 3～7 段依問答 SSE，`sources`／`done` 有後端的
   `pipeline`（ADR 030）時，通過／擋下／略過一律照它，完成時沒列到的段落是「沒有執行」。前端沒有 Jev／地端切換。
6. **瀏覽器儲存**（`shell/persist.ts`，key `artrag-shell-v1`）：對話紀錄、最後進過的模組、各模組正在看的成果、側欄收合、
   分隔線位置、入口深淺色。只存公開資料（畫作問答的回答與公開段落、以文搜畫結果）與後端遮蔽個資後的問句；
   不存 JWT（HttpOnly cookie，JavaScript 讀不到）、`route_ticket`、JWT payload、送給 Jev 的請求本文與代號對照、
   第 4 段剔除的段落、本機檔案（照片先上傳，只存 `image_id`）。工廠內部資料只存編號（圖紙 id、3D 工作編號、排程結果編號），
   重新整理後展示區以目前的 JWT 重新讀取，查詢結果要「以目前身分重新查詢」。
   被關卡拒絕的一輪（第 1、2 段擋下、降級「查無資料」、試算被拒絕、閘道 401）連問句都不存，只存佔位文字與流程摘要，
   也不能拿佔位文字重送；其餘問句與回答只要提到帳密類關鍵字（密碼、金鑰、token、api key、credentials、private key…；程式識別字先拆成單字再比對，
   `secret_key`、`SECRET_KEY`、`secretKey`、`clientSecret`、`x-api-key` 都算，`secretary`、`tokenizer` 不算），整段不保存，
   只存佔位文字（`sanitizeForStorage`）——值的邊界（多行、YAML 區塊、JSON 陣列、跳脫引號）無法逐一解析完整，
   寧可整段不存；沒有關鍵字的文字再遮掉 JWT、金鑰前綴、PEM 區塊與 32 字以上的不透明字串。
8. **切換身分**：不推定某個請求帶的是哪一張 JWT，改用「身分世代」：
   - 身分未確認（剛打開頁面 `/auth/accounts` 還沒回來、`client.ts` 發出 `artrag:token-renewed` 之後）：輸入框鎖住，
     不接受提問、重新查詢與任務，`/agent/route` 的回應一律不採信；憑證更新時世代加一、中止在飛的請求、清掉快取，
     而且所有對話裡已經完成（或停止、出錯）的非公開成果立刻收起成流程摘要與編號（`redactPrivate`），不等身分重新確認；
     憑證更新前快取裡的那份 `/auth/accounts` 回應不能拿來重新確認身分。
     第一次確認身分也和換身分一樣處理（中止、清快取、收起其他身分的內容），不假定確認前沒有請求。
   - 開始切換（`useSwitchAccount` 發出 `artrag:account-switching`）：世代加一，所有在飛的請求與任務（第 1～7 段、3D、排程、寫入）
     中止並收起，切換完成前輸入框鎖住、不接受新提問與任務。
   - 切換完成（`artrag:account-switched` 帶新身分）或 `/auth/accounts` 回來的身分改變（憑證過期改發訪客）：世代再加一，
     和身分有關的 React Query 快取 `resetQueries`（清掉舊資料再重抓，看不到的停在 403），其他身分查到的非公開內容收起。
   - 寫回時再核對：每次執行只在開始時的世代仍有效時寫回；`/agent/route` 回來的 `account` 不是目前身分就不寫回、不分派。
   - 展示區的檢視一律「錯誤優先」：重抓被拒絕時不再顯示快取裡的舊資料。
9. **七段軌跡只看真實回應**：搜尋、比較、Text-to-SQL、3D、排程、試算各段，收到成功回應才是通過；等待中、失敗（擋下）、
   停止或出錯後沒有結果的段落（沒有執行）各自標出，不會預先標成通過。
7. **樣式**：`styles/core.css`、`app.css` 是整合版的結構；`entry.css`、`module.css`、`art.css` 依畫面只掛該用的那幾份
   （`shell/theme.ts`）。全部放在 CSS 層 `artrag`，排在 Tailwind 的 `base` 之後、`utilities` 之前：功能頁的 utility 不會被
   整合版的元素重設蓋掉；Tailwind 的色票接到整合版的 token，功能頁跟著模組變色。規則見 `frontend/DESIGN.md`。

## API 契約缺口（本次不改後端，只記錄最小建議）

- **細看的畫面座標標註**：整合版的「細看」在畫上標 6 個編號重點、點了放大到那一處，現有 API 沒有座標。
  這次改用知識庫段落（依主題）＋色塊分布圖疊圖＋自由縮放，不畫熱點。建議 `ArtworkDetail` 加
  `highlights: [{ x, y, zoom, topic, text, source }]`（x、y 為百分比）。
- **畫家生平年表**：沒有畫家生平事件的 API，年表只列知識庫裡的作品與「創作背景」段落。建議 `GET /artists/{id}/timeline`。
- **作品之間的相似度**：沒有「以作品找作品」的端點，相似作品用作品的風格標籤＋媒材做以文搜畫（排除本作）。
  建議 `GET /artworks/{id}/similar`（直接用索引裡的 CLIP 向量）。
- **追問上下文**：`/agent/route` 沒有上下文欄位，模組裡的指代靠把對象名稱帶進問句。建議 `RouteRequest` 加
  `context: { part_id?, artwork_id? }`，由後端一樣做第 1 段授權。

## 取捨

- 重新整理後工廠成果要重新讀取或重新查詢：換來瀏覽器裡沒有任何內部資料，換人使用這台電腦也看不到。
- 功能頁沿用原本的 Tailwind 元件，只換色票與外框，不逐頁重畫；所有功能（照片建檔、批次辨識、核准、管理）都保留。
- 驗證：`npm test`（35 項，jsdom＋mock transport，不連後端）、`frontend/scripts/smoke.mjs`（真瀏覽器、本機 mock 後端，
  1440×900 與 390×844）。
