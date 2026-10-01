# ADR 005：生產排程採 Timefold Solver（Java 微服務），與圖紙、工廠資料庫整合

- 日期：2026-10-01
- 狀態：採用（demo）

## 背景
工廠圖紙領域已經有「圖紙」（辨識、Ortho2CAD 3D 重建、製程問答，ADR 003）與「資料庫」（庫存、訂單、工單，
Text-to-SQL，ADR 004）。現場的下一步是：看到某張圖紙缺貨 → 開工單 → 決定哪台機台什麼時候做。
這是典型的**彈性零工式排程（flexible job shop）**：每張工單依圖紙的途程有好幾道工序、各工序只能在特定機型做、
工序有先後、熱處理與表面處理委外要等幾天、換不同零件要換線，目標是準時交貨、少換線、早點做完。
這類組合最佳化問題用 LLM 做不可靠也無法驗證，業界標準做法是限制求解器（constraint solver）。

## 選項
1. **Timefold Solver Java 版**（OptaPlanner 的後繼，Apache-2.0，持續維護；2.7.0 需 Java 21）：
   以獨立服務執行，後端用 HTTP 呼叫。
2. Timefold Python 套件：官方已停止維護（最後一版 2025-07 的 1.24.0b0），JVM 以 JPype 嵌在後端行程，
   載入後無法釋放記憶體。
3. 不用求解器，自己寫 Python 啟發式：沒有 Timefold 的增量計分與局部搜尋，也無法展示「規劃實體／限制條件」的架構。
4. OR-Tools CP-SAT：適合純數學模型，但限制條件寫成數學式，不如 Constraint Streams 好讀好改。

## 決定
採用 1。`scheduler/` 是一個小型 Java 21 服務（Timefold Solver 2.7.0＋JDK 內建 HTTP 伺服器＋Jackson，
fat jar 約 10 MB），只聽 127.0.0.1:8082，和 Ortho2CAD 的 llama-server（:8081）一樣由 Makefile 啟動。
Java 與 Maven 的安裝見 `make scheduler-setup`（Maven 從 Maven Central 下載並驗證 SHA-512，不用 brew 的 maven，
因為它會再裝一套最新版 JDK）。

### 整合方式（圖紙 → 資料庫 → 排程 → 資料庫）
```
圖紙 kb/parts「加工製程」段落 ─► kb/production/routings/<id>.json（工序、機型、準備＋每件工時、委外天數）
圖紙頁「生產工單」卡 ─► 依庫存（可用＋生產中－未出貨－安全庫存）建議數量與交期 ─► 開立工單
     └─► data/production.sqlite3（可寫入：使用者工單、每次排程結果）
排程頁 ─► 後端 build_problem()：既有工單（kb/inventory）＋開立的工單 × 途程 × 機台 → 工序
     ─► Timefold（:8082）求解，每找到更好的解就串流到前端重畫甘特圖 ─► 結果寫回 production.sqlite3
     ─► 工廠資料庫 inventory.sqlite3 自動重建：work_orders 含新工單、schedule_ops、v_wo_plan
     ─► Text-to-SQL 可以問「哪些工單會趕不上交期」「CNC 車床-01 接下來排了什麼」
```
模型產生的 SQL 仍只碰唯讀的 inventory.sqlite3（ADR 004 的三道防護不變）；可寫入的 production.sqlite3
只有系統自己寫。兩個檔案靠 `production_repo.revision()` 連動：每次寫入遞增，工廠資料庫的 seed hash 包含它。

### Timefold 模型
- **規劃解** `ProductionSchedule`：工單（fact）、機台與工序（entity）、`HardMediumSoftScore`
- **機台** `Machine`：`@PlanningListVariable List<Operation>`——Timefold 決定每台機台做哪些工序、什麼順序；
  每台機台的可選範圍是同機型的工序（entity 的 `@ValueRangeProvider`）；生產中的工序事先放在最前面並以
  `@PlanningPinToIndex` 釘選
- **工序** `Operation`：`@InverseRelationShadowVariable`（機台）、`@PreviousElementShadowVariable`（同機台前一道），
  起訖時間是**宣告式影子變數**（`@ShadowVariable(supplierName)`＋`@ShadowSources`）：
  開始＝max(工單可開工、同機台前一道完成、同工單前一道完成＋委外天數)，完成＝開始＋換線＋加工；
  同機台前後是同圖紙同工序就免換線。機台順序與工序先後互相矛盾（循環）時，Timefold 2.x 以 structural score 處理
- **限制條件**（Constraint Streams）：硬＝機型不符；中＝交期延遲（延遲工作分鐘 × 權重，急件 3 倍）；
  軟＝換線準備分鐘＋各工單完工時間
- **求解**：建構初始解 → 局部搜尋；預設 20 秒或連續 8 秒沒有更好的解就停（`models.yaml` 的 `scheduling`）

### 時間與行事曆
排程引擎只看「工作分鐘」（排程起點起算、只計上班時間），行事曆換算在後端（`app/scheduling/calendar.py`）：
週一至週五 08:00–12:00、13:00–17:00，2026-10-09 國慶日補假。好處是 Java 端完全不需要處理午休、週末與假日，
委外天數也以工作天計。交期＝交期當天下班。排程起點是庫存資料日期的下一個工作日（2026-10-01 08:00）。

### 分數明細與核對
Timefold 2.x 的 Score analysis（各限制條件的分數明細）屬商業版功能，社群版呼叫會丟例外。所以 Java 只回總分，
各條件明細由後端 `app/scheduling/solution.py` 用同一套規則重算，並**核對總分與 Timefold 一致**（前端顯示 ✓）；
同一份程式也是 Timefold 不可用時的簡易排程（交期優先派工）的計分，以及 pytest 的時間推算核對。

### 沒有 Java 的電腦
排程服務連不上時排程頁自動改用簡易排程，並標示「未最佳化」；`SCHEDULER_MODE=mock` 一律用簡易排程（CI）。

## 結果（MacBook Air M5，2026-10-01）
示範情境：既有 4 張未完工工單（3 張生產中）＋圖紙頁開立 3 張（連接法蘭 80 件急件、L 型固定支架 120 件、
階梯傳動軸 50 件），共 7 張工單、30 道工序、11 台機台。

| 方法 | 分數（硬／中／軟） | 延遲 | 換線 | 時間 |
|---|---|---|---|---|
| 簡易排程（交期優先派工） | 0／-423／-31,794 | 1 張（L 型固定支架）、423 分鐘 | 24 次 | < 0.1 秒 |
| Timefold 建構初始解 | 0／-3,819／-32,540 | 有延遲 | — | 0.03 秒 |
| Timefold 局部搜尋 | 0／0／-22,939 | 0 | 20 次、8.8 小時 | 約 1 秒找到，8.6 秒結束 |

後端重算的分數與 Timefold 逐位一致；工序起訖的推算（Java 影子變數 vs Python simulate）也逐道一致。
JVM 閒置約 90 MB RSS（SerialGC、heap 上限 768 MB）。

⚠️ 機台、工時與委外天數是虛構的示範資料，數字只證明流程可行。

## 影響
- 正式版：production schema 放在同一個 PostgreSQL，Text-to-SQL 的唯讀角色只能讀排程結果的檢視表；
  排程服務可改用 Timefold 的 Quarkus／Spring Boot 整合，或部署在 5070 Ti 主機
- 想加限制條件（例如人員、夾治具、機台保養時段）：在 `ProductionConstraints` 加一條，並在
  `solution.evaluate()` 加同樣的規則（總分核對會提醒兩邊不同步）
- 行事曆、急件權重、機台在 `kb/production/site.json`；新圖紙加一份 `kb/production/routings/<id>.json` 即可排程
