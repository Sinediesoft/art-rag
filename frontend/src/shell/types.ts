import type {
  ArtworkSummary,
  ChangeCommitted,
  ChangePreview,
  Approval,
  ItemComparison,
  PartSummary,
  RouteResponse,
} from "../api/client";
import type {
  CadDoneEvent,
  CadMetaEvent,
  CadResultEvent,
  DoneEvent,
  ErrorEvent,
  ScheduleDoneEvent,
  ScheduleMetaEvent,
  ScheduleProgressEvent,
  ScheduleSolutionEvent,
  SourcesEvent,
  SqlAttemptEvent,
  SqlDoneEvent,
  SqlResultEvent,
} from "../api/sse";
import type { Domain, View } from "./design";

/**
 * 一輪問答的狀態：問句先送 /agent/route（第 1、2 段），再依分派呼叫真實 API／SSE（第 3～7 段與各模組）。
 * 所有欄位都來自後端回應；前端只決定要怎麼顯示，不產生任何通過、拒絕或結果。
 * 執行由 shell/runner.ts 負責、狀態存在 shell/store.tsx，換畫面或到功能頁再回來都不會中斷或重送。
 */
export type Phase = "routing" | "running" | "done" | "stopped" | "error";

export interface ApiFailure {
  code: string;
  message: string;
  requestId: string;
  /** HTTP 狀態；0＝連不上伺服器 */
  status: number;
}

export type ChatPart = {
  kind: "chat";
  target: { artwork_id?: string; part_id?: string };
  status: "retrieving" | "streaming" | "done" | "error" | "stopped";
  sources: SourcesEvent | null;
  text: string;
  done: DoneEvent | null;
  error: ErrorEvent | null;
};

export type SqlPart = {
  kind: "sql";
  question: string;
  status: "generating" | "executing" | "answering" | "done" | "error" | "stopped";
  attempts: SqlAttemptEvent[];
  draft: string;
  result: SqlResultEvent | null;
  answer: string;
  done: SqlDoneEvent | null;
  error: ErrorEvent | null;
};

export type ArtSearchPart = {
  kind: "artSearch";
  q: string;
  /** 依 Chinese-CLIP＋bge-m3 的分數排序（/search/text） */
  status: "loading" | "done" | "error";
  items: { artwork: ArtworkSummary; score: number }[];
  error: ApiFailure | null;
};

export type PartSearchPart = {
  kind: "partSearch";
  q: string;
  status: "loading" | "done" | "error";
  items: { part: PartSummary; score: number }[];
  /** 不在資料範圍、檢索時就被濾掉的張數（第 3 段 Metadata Filter） */
  hidden: number;
  filter: string | null;
  error: ApiFailure | null;
};

export type CadJob = {
  status: "preparing" | "generating" | "executing" | "done" | "error" | "stopped";
  meta: CadMetaEvent | null;
  code: string;
  result: CadResultEvent | null;
  done: CadDoneEvent | null;
  error: ErrorEvent | null;
  startedAt: number;
};

export type ReconstructPart = {
  kind: "reconstruct";
  partId: string | null;
  partLabel: string | null;
  imageId: string | null;
  /** 按「開始轉換」才建立（約 1–2 分鐘、載入約 6 GB 模型，不自動執行） */
  job: CadJob | null;
  cancelled: boolean;
};

export type SolveJob = {
  status: "starting" | "solving" | "done" | "error" | "stopped";
  meta: ScheduleMetaEvent | null;
  progress: ScheduleProgressEvent | null;
  solution: ScheduleSolutionEvent | null;
  done: ScheduleDoneEvent | null;
  error: ErrorEvent | null;
  elapsedMs: number;
};

export type SchedulePart = {
  kind: "schedule";
  /** 按「開始排程」才求解（結果寫回資料庫，不自動執行） */
  job: SolveJob | null;
  cancelled: boolean;
};

export type ChangePart = {
  kind: "change";
  /** unconfirmed：寫入已經送出、沒有收到伺服器的回覆（連線中斷、閘道逾時）——可能已經寫好，不能當成失敗重送 */
  status: "previewing" | "ready" | "committing" | "unconfirmed" | "error";
  /** 送出的是確認寫入還是送主管核准 */
  action?: "commit" | "approval";
  preview: ChangePreview | null;
  committed: ChangeCommitted | null;
  approval: Approval | null;
  error: string | null;
};

export type ComparePart = {
  kind: "compare";
  refs: string[];
  labels: string[];
  status: "loading" | "done" | "error" | "single";
  data: ItemComparison | null;
  error: ApiFailure | null;
};

/** 不需要再呼叫其他 API 的分派：閒聊短路、擋下、澄清、超出範圍、系統狀態、批次辨識說明、比對不到的照片 */
export type RoutePart = { kind: "route" };

export type Part = ChatPart | SqlPart | ArtSearchPart | PartSearchPart | ReconstructPart | SchedulePart | ChangePart | ComparePart | RoutePart;

export interface Turn {
  id: string;
  /** 使用者輸入（送出前）；有 route 之後畫面改顯示 route.question（已遮蔽個資） */
  text: string;
  imageId: string | null;
  /** 點澄清按鈕選的意圖 */
  forced: string | null;
  /** 在入口或哪個模組問的 */
  at: View;
  ts: number;
  /** 示範第 1 段：用竄改過的 JWT 送出（憑證本身不保存） */
  tamper?: boolean;
  account: { id: string; label: string } | null;
  phase: Phase;
  route: RouteResponse | null;
  /** /agent/route 本身失敗（401 閘道拒絕、403、連不上、伺服器錯誤） */
  failure: ApiFailure | null;
  part: Part | null;
  /** 從瀏覽器紀錄還原、內容沒有保存（工廠內部資料、被擋下的請求）：只剩問句與當時的流程摘要 */
  archived?: ArchivedTurn;
}

/** 存進瀏覽器的一輪（shell/persist.ts 的 snapshot） */
export interface ArchivedTurn {
  intentLabel: string | null;
  domain: Domain | null;
  outcome: string | null;
  /** 七段當時的狀態（只有名稱與通過／擋下，不含檢查細節與送給 Jev 的內容） */
  stages: { key: string; short: string; state: string }[];
  summary: string | null;
  /** 公開資料：畫作問答的回答與公開段落、以文搜畫結果 */
  artworkId?: string | null;
  artworkLabel?: string | null;
  answer?: string;
  sources?: SourcesEvent["sources"];
  artResults?: { artwork: ArtworkSummary; score: number }[];
  /** 這一輪有沒有內容因為不是公開資料而沒有保存 */
  redacted: boolean;
  /**
   * 工廠成果的「編號」（不含名稱、數字或內容）：重新打開紀錄時展示區照編號以目前的 JWT 重新讀取，
   * 看不到的由後端回 403；查詢結果沒有保存，要以目前身分重新查詢
   */
  refs?: { partId?: string; cadJobId?: string; scheduleRunId?: string; schedule?: boolean; sql?: boolean };
}

export interface Conv {
  id: string;
  createdAt: number;
  updatedAt: number;
  turns: Turn[];
  /** 最後進過的模組：點紀錄時直接回到那個模組看成果（沒進過就回到入口） */
  module?: Domain;
  /** 各模組展示區正在看的成果 */
  active: Partial<Record<Domain, string>>;
  /** 系統提示（切換身分等），依時間插在對話裡 */
  notices: { id: string; ts: number; text: string }[];
}
