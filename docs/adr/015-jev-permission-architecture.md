# ADR 015：七段權限控管——JWT、Jev Choice、Metadata Filter、Jev Noul、Jev Score、生成閘門、本地 LLM

- 日期：2026-10-03
- 編號：原 ADR 013，2026-10-03 合併 `dev` 時改號——012 是 `dev` 的影像對位（`012-image-alignment-compare.md`）、013 已被 `feat/照片建檔` 的 `013-photo-intake.md` 使用
- 狀態：採用（demo，`d-rag`）；2026-10-06 由 ADR 019 修補實作（伺服器強制每一段、單一圖紙可見性函式、
  地端 hard-block 不可被 Jev 覆寫、地端閘門實際檢查、第 7 段輸出檢查、`DEMO_CONTROLS` 預設關閉），
  下文「第 2 段 Jev 有回答就不跑地端規則」「其他頁面沒帶 `post_filter` 只掃描」「地端閘門有段落或文件卡就放行」已不適用。
  取代 ADR 014 的五段流程。ADR 014 的個資遮蔽、資料範圍（`access.yaml` 的 `clearance`）、
  示範用汙染資料、拒絕並記錄都沿用；ADR 011 的修改資料流程與主管核准不變
- 來源：專題資料夾的 `地端RAG_Jev權限控管標準架構.html`（7 步流程圖），使用者要求「依圖示架構修正 d-rag 流程」

## 背景
ADR 014 的五段（RBAC → Jev 護欄 → Metadata Filter → Jev 過濾 → 地端生成）和標準架構圖有四個差別：

1. **身分不是憑證**：身分存在伺服器記憶體的工作階段（`artrag_sid`），沒有「Token 效期」可驗，也沒有「未授權直接拒絕連線」——
   沒有工作階段就當訪客。
2. **Jev 只判斷有沒有惡意**：沒有分出「無關閒聊」，閒聊照樣走一次本地分流與回覆。
3. **驗證與重排混在一起**：第 4 段一次判斷注入＋關聯性，沒有評分重排（Jev Score），也沒有「段落有沒有洩密風險」這個角度。
4. **沒有生成閘門**：權限內找不到答案時，仍把最接近的段落丟給 LLM（「至少留一段，拒答交給生成端」）；
   業務問機密圖紙時，第 1 段直接說「〈連接法蘭〉是機密文件，切換成倉管再試」——等於告訴他這份文件存在。

## 決定

```
用戶提問＋JWT
→ 1. 認證與授權                                   services/identity.py gateway()、agent/guard.py auth_check()
     API 閘道驗 JWT（HS256）：簽章、效期、簽發者、kid；沒帶、竄改、過期 → 401，請求碰不到任何模型與資料
     （簽章不符寫進拒絕並記錄）。通過後用憑證的角色檢查「要做的事」：功能（資料領域）＋動作權限
     ❌ 角色不符 → 拒絕並記錄、「切換成〇〇再試一次」；指定了看不到的圖紙（3D、圖紙查找）→ 降級「查無資料」
→ 2. Jev 意圖路由／防護欄（Jev Choice）           agent/guard.py guard_input()
     本地分流（關鍵字＋bge-m3）決定交給哪個模組；Jev Choice 三選一：正常查詢／Prompt 注入或越權／無關閒聊
     ❌ 注入 → 拒絕並記錄；💬 閒聊 → 快速短路回覆（不檢索、不生成）
→ 3. 權限感知檢索（Metadata Filter）              services/chat_service.py retrieve()
     條件只照 JWT 產生：domain AND clearance <= 憑證等級 AND dept IN ("公開", 憑證的 depts…) [AND doc_id]
     在資料庫查詢階段過濾（檔案索引的 owners、PostgreSQL 的 WHERE），看不到的文件塊不會進候選
→ 4. Jev Noul 雙重驗證                            agent/guard.py process_passages()
     每段 ① is_relevant ② security_leak_check，任一不通過就剔除；剔除的洩密段落寫進拒絕並記錄
→ 5. Jev Score 評分重排
     通過的段落評 0～3 分（無關／背景／部分／直接），低於 1 分不放，取前 3 段，取代傳統的 Reranker
→ 6. 生成閘門 Generation Gate（Jev Noul）
     answerable（權限內的資料能不能回答）＋ compliant（回答是否合規）
     🔒 不過 → 降級回應「查無資料：在你目前可以查閱的資料中，找不到能回答這個問題的內容。」，不呼叫 LLM
→ 7. 本地 LLM 生成（Qwen3-VL，System 2）只依第 5 段留下的段落回答；3D、排程、修改交給各自的地端模組
```

### 1. 身分改成 JWT（`app/core/jwt.py`、`services/identity.py`）
- 切換身分（`POST /auth/switch`）時簽發 JWT：`iss`、`sub`、`name`、`roles`、`dept`、`depts`、`clearance`、`iat`、`exp`
  （預設 8 小時，`.env` 的 `JWT_TTL_MIN`）。放在 HttpOnly cookie `artrag_token`，API 也收 `Authorization: Bearer`（優先）。
- 所有 `/api/v1` 請求先過閘道（`main.py` 的 middleware）。不用憑證的只有 `/health`、`/auth/accounts`、`/auth/switch`；
  `/auth/accounts` 沒有有效憑證時直接改發預設帳號（訪客）的憑證——這是展示版的「登入」。
- 不另裝套件：HS256 用標準函式庫實作（`hmac.compare_digest`）；header 的 `alg` 不是 HS256 一律拒絕（擋 `alg=none`）。
- 金鑰 `JWT_SECRET` 留空時每次啟動隨機產生：後端重啟後舊憑證失效。header 帶 `kid`（金鑰指紋）區分兩種 401：
  `TOKEN_STALE`（舊金鑰簽發＝後端重啟過，前端自動重新取得、不記錄）與 `TOKEN_INVALID`（金鑰沒變但簽章不符＝竄改，寫進拒絕並記錄）。
- 權限（ops）仍照 `access.yaml` 的角色；Metadata Filter 只看憑證的 `clearance` 與 `depts`。
- 前端：`api/client.ts`、`api/sse.ts` 遇到 `UNAUTHENTICATED`／`TOKEN_EXPIRED`／`TOKEN_STALE` 先向 `/auth/accounts`
  取新憑證再重送一次；頁首顯示「JWT · clearance · 部門 · 到期時間」，點開看閘道驗了什麼與 payload（簽章不回傳到畫面）。

### 2. 部門（`access.yaml`）
架構圖的 Metadata Filter 有部門條件，這次補上：每個角色有 `dept`（自己的部門，寫進 JWT）與 `depts`（讀得到哪些部門的文件）；
圖紙的部門就是 `kb/parts/*.json` 的 `owner`（生產技術課、機械設計課、自動化設備課），畫作是「公開」。
目前除了訪客以外，各角色的 `depts` 都是三個課，所以**誰看得到哪些圖紙和 ADR 014 一樣**（業務 clearance 1 看不到機密）；
要讓某個角色只看自己課的圖紙，改 `depts` 即可。

### 3. 第 2 段：Jev Choice
題目只有一題 `intent_guard`（choice：`query`／`attack`／`chitchat`，說明在 `agent.yaml` 的 `intent_classes`）。
注入、閒聊的機率要 ≥ 0.5 才採信，都不到就當正常查詢（只提醒）。冒充身分、聲稱已獲同意來跳過權限或核准，歸在 `attack`。
ADR 014 的 `prompt_attack`／`overrides_rules`／`risk_class` 三題拿掉；叫不到 Jev 時的地端規則不變（已知注入樣式擋下、
冒充身分＋耗時工作或修改資料擋下），另外本地分流判為「超出範圍」就當閒聊短路。

### 4. 第 4～6 段的隱私取捨（不變）
只有**公開段落**（畫作）代號化後送 Jev；工廠圖紙的內部、機密段落一律不出廠，改在地端判斷：
- 第 4 段：security_leak_check 用地端規則（`local_rules.indirect`＋新增 `local_rules.leak`：要求附上內部電話、成本、帳密）；
  is_relevant 由本地 Qwen3-VL 判斷（段落篩選，ADR 008；它說「都沒幫助」時全部剔除），沒開或失敗時看相似度（≥ 0.30）
- 第 5 段：依相似度排序
- 第 6 段：有段落通過、或已指定看得到的圖紙（圖紙資料與圖面可回答基本問題）才放行；完全沒有就降級
送 Jev 的內容：第 4 段＝問題＋公開段落；第 5 段＝通過的公開段落；第 6 段＝問題＋作品資料＋留下的段落。三次請求共用同一套代號。

### 5. 不透露有沒有這份文件
- 業務問「連接法蘭有哪些公差要求？」：第 1 段功能授權照樣通過（業務可以用工廠圖紙）、`filter` 不回傳看不到的文件等級；
  第 3 段 `clearance <= 1` 濾掉機密圖紙 → 0 段候選 → 第 6 段降級「查無資料」。回應裡不出現「機密」，也不建議換誰。
- 3D 重建、圖紙查找指定了看不到的圖紙：第 1 段直接降級「查無資料」（`outcome=degraded`），一樣不建議換誰。
- 其他頁面直接打 API（`/parts/{id}`、沒帶 `post_filter` 的 `/chat`）仍回 403 `DATA_SCOPE_DENIED`（ADR 014 的決定：資料範圍套全系統）。
- 庫存、訂單、工單是工廠資料庫的權限，和圖紙的機密等級無關：業務問「法蘭還剩幾件」照常查詢
  （ADR 014 的第 1 段會因為「法蘭是機密圖紙」把業務擋下，這次一併修正）。

## 評估（2026-10-03，Mac 本機，`jev-1.13.0`）
| | 地端規則 | Jev |
|---|---|---|
| `make eval-route`（44 句）意圖正確率 | 100% | 100% |
| 第 2 段誤擋／誤判成閒聊（44 句正常請求） | 0／0 | 0／0 |
| `make eval-guard` 攻擊攔截率（12 句） | 50%（換句話說的注入 0/4） | 100%（12/12） |
| `make eval-guard` 正常請求誤擋率（12 句） | 0% | 0% |
| `make eval-guard` 閒聊短路率（6 句，新增） | 100%（本地分流判為超出範圍） | 100% |
| 第 2 段延遲 p50 | 0 ms | 約 255 ms |
| 第 4～6 段（`eval/qa.jsonl` 19 題畫作問答，不含生成） | — | 可答題誤降級 0/18、不可答題正確降級 1/1、標準段落留下 18/18；p50 約 0.8 秒（3 次 Jev） |

第 4～6 段的逐題結果在 `eval/runs/experiments/20261003-seven-stage-gate.jsonl`。
實測（真 Jev＋本機 Qwen3-VL 4B）：
- 主管問「有絲柏的麥田是在聖雷米的療養院附近畫的嗎？」：Jev Noul 剔除觀眾留言（security_leak_check 0.97）與 2 段無關段落，
  Jev Score 留「創作背景」2.89、「基本資料」1.70，閘門可答 0.86 → 回答正確並引用 [1]；外送 6.9 KB（3 次 Jev），總計約 8 秒
- 主管問「有絲柏的麥田現在市價多少？」：5 段都判為無關 → 閘門可答 0.03 → 「查無資料」，1.8 秒、沒有呼叫 LLM
- 倉管問「連接法蘭有哪些公差要求？」：外包廠回報被地端規則剔除，Qwen3-VL 留下「公差與檢驗」，地端閘門放行，外送 0
- 業務問同一句：0 段候選 → 地端閘門降級，0.1 秒，外送 0（第 2 段 Jev Choice 的 1.2 KB 除外）

注意：`guard_qa.jsonl` 的閒聊 6 句是這次自己寫的；`qa.jsonl` 只有 1 題不可答題，生成閘門的「該降級卻放行」還需要補題目。

## 取捨
- 每個畫作問答多 2 次 Jev 呼叫（第 5、6 段，各約 0.3 秒）、外送多約 3 KB；換到的是重排與「權限內查無答案就不生成」
- 第 2 段只剩一題：冒充身分但只是查詢（「我是主管，法蘭還剩幾件？」）不再單獨提醒，由 Jev Choice 判斷
- 業務問機密圖紙不再看到「切換成〇〇再試一次」：展示時要說明這是「不透露有這份文件」的設計
- `JWT_SECRET` 留空時後端重啟就要重新取得憑證（前端自動處理，但身分會回到訪客，和工作階段時代一樣）
- 機密段落的第 4～6 段在地端判斷：換句話說的洩密內容認不出來（地端規則只認得已知樣式）
