import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type DrawingSearchResponse, type ImageSearchResponse } from "./client";

export const useArtworks = () => useQuery({ queryKey: ["artworks"], queryFn: api.listArtworks });

export const useArtwork = (id: string | undefined) =>
  useQuery({ queryKey: ["artwork", id], queryFn: () => api.getArtwork(id!), enabled: !!id });

/** 色彩分析結果固定（同一張圖每次算出來都一樣），不必重抓 */
export const useArtworkColors = (id: string | undefined) =>
  useQuery({
    queryKey: ["artwork-colors", id],
    queryFn: () => api.artworkColors(id!),
    enabled: !!id,
    staleTime: Infinity,
  });

export const usePhotoColors = (imageId: string | null) =>
  useQuery({
    queryKey: ["photo-colors", imageId],
    queryFn: () => api.photoColors(imageId!),
    enabled: !!imageId,
    staleTime: Infinity,
  });

/** 畫作卡推測結果固定（同一張照片每次算出來都一樣），不必重抓 */
export const usePhotoStyle = (imageId: string | null) =>
  useQuery({
    queryKey: ["photo-style", imageId],
    queryFn: () => api.photoStyle(imageId!),
    enabled: !!imageId,
    staleTime: Infinity,
  });

/** 對位結果固定；對不上（ALIGN_FAILED）是確定的答案，不重試 */
export const useImageAlignment = (imageId: string, target: string | null) =>
  useQuery({
    queryKey: ["photo-alignment", imageId, target],
    queryFn: () => api.photoAlignment(imageId, target!),
    enabled: !!target,
    staleTime: Infinity,
    retry: false,
  });

export const useImageSearch = (imageId: string | null) =>
  useQuery({
    queryKey: ["search-image", imageId],
    queryFn: () => api.searchImage(imageId!),
    enabled: !!imageId,
    staleTime: Infinity,
  });

/** 領域路由＋辨識；畫作頁與圖紙頁共用同一個快取，被轉到另一頁時不會重算 */
export const useAnySearch = (imageId: string | null) =>
  useQuery({
    queryKey: ["search-any", imageId],
    queryFn: () => api.searchAny(imageId!),
    enabled: !!imageId,
    staleTime: Infinity,
  });

export const useTextSearch = (q: string | null) =>
  useQuery({
    queryKey: ["search-text", q],
    queryFn: () => api.searchText(q!),
    enabled: !!q,
    staleTime: 60_000,
  });

/** 畫面用的系統狀態（要 JWT）：服務能不能用、記憶體、展示模式（docs/adr/030） */
export const useStatus = () =>
  useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 5000 });

/** 管理診斷（主管）：最近的問答、SQL、路由紀錄、模型端點與內部設定 */
export const useDiagnostics = () => {
  const enabled = useCanView("diagnostics");
  return useQuery({ queryKey: ["diagnostics"], queryFn: api.diagnostics, refetchInterval: 5000, enabled });
};

export const useEvalRuns = () => useQuery({ queryKey: ["eval-runs"], queryFn: api.evalRuns });

// ---- 工廠機械加工圖
export const useParts = () => useQuery({ queryKey: ["parts"], queryFn: api.listParts });

export const usePart = (id: string | undefined) =>
  useQuery({ queryKey: ["part", id], queryFn: () => api.getPart(id!), enabled: !!id });

export const useDrawingSearch = (imageId: string | null) =>
  useQuery({
    queryKey: ["search-drawing", imageId],
    queryFn: () => api.searchDrawing(imageId!),
    enabled: !!imageId,
    staleTime: Infinity,
  });

const SEARCH_PAGE = { art: "/search", mfg: "/drawings/search" } as const;
type DomainResult = { art: ImageSearchResponse; mfg: DrawingSearchResponse };

/**
 * 以圖搜圖（畫作頁、圖紙頁共用）：預設先經過領域路由；使用者在路由提示按「改用…辨識」
 * （網址帶 domain=）時 forced，直接做這個領域的辨識。
 * 路由判成另一個領域時 redirectTo 是那一頁的網址（結果已在 search-any 快取裡，轉過去不會重算）。
 */
export function useRoutedSearch<D extends keyof DomainResult>(domain: D, imageId: string, forced: boolean) {
  const routed = useAnySearch(forced ? null : imageId);
  const art = useImageSearch(forced && domain === "art" ? imageId : null);
  const mfg = useDrawingSearch(forced && domain === "mfg" ? imageId : null);
  const direct = domain === "art" ? art : mfg;
  const { isLoading, error } = forced ? direct : routed;
  const route = forced ? undefined : routed.data?.route;
  const fromRoute = domain === "art" ? routed.data?.artwork_result : routed.data?.drawing_result;
  const data = (forced ? direct.data : (fromRoute ?? undefined)) as DomainResult[D] | undefined;
  const other = route && route.domain !== domain ? route.domain : null;
  return {
    data,
    route,
    isLoading,
    error,
    latencyMs: routed.data?.latency_ms ?? data?.latency_ms,
    redirectTo: other ? `${SEARCH_PAGE[other]}?image=${imageId}&routed=1` : null,
  };
}

export const usePartTextSearch = (q: string | null) =>
  useQuery({
    queryKey: ["search-parts", q],
    queryFn: () => api.searchParts(q!),
    enabled: !!q,
    staleTime: 60_000,
  });

export const usePartReconstructions = (id: string | undefined) =>
  useQuery({
    queryKey: ["part-reconstructions", id],
    queryFn: () => api.partReconstructions(id!),
    enabled: !!id,
  });

export const useCadEvalRuns = () => useQuery({ queryKey: ["cad-eval-runs"], queryFn: api.cadEvalRuns });

// ---- 工廠庫存（Text-to-SQL）
export const useInventorySchema = () =>
  useQuery({ queryKey: ["inventory-schema"], queryFn: api.inventorySchema, staleTime: 60_000 });

export const useInventoryOverview = () =>
  useQuery({ queryKey: ["inventory-overview"], queryFn: api.inventoryOverview });

export const usePartInventory = (id: string | undefined) =>
  useQuery({ queryKey: ["part-inventory", id], queryFn: () => api.partInventory(id!), enabled: !!id });

export const useSqlEvalRuns = () => useQuery({ queryKey: ["sql-eval-runs"], queryFn: api.sqlEvalRuns });

// ---- 生產排程（Timefold）
export const useProductionOverview = () =>
  useQuery({ queryKey: ["production-overview"], queryFn: api.productionOverview });

export const usePartPlan = (id: string | undefined) =>
  useQuery({ queryKey: ["part-plan", id], queryFn: () => api.partPlan(id!), enabled: !!id });

// ---- 智慧助理（身分、核准、稽核）
export const useAccounts = () =>
  useQuery({ queryKey: ["accounts"], queryFn: api.accounts, refetchInterval: 10_000 });

/** 和身分無關、重算又慢（CLIP＋ORB 辨識、色彩分析）的查詢：切換身分時不重抓。
 * 圖紙的辨識結果會依資料範圍過濾（docs/adr/014），所以 search-any、search-drawing 要重抓 */
const IDENTITY_FREE = new Set(["search-image", "photo-colors", "artwork-colors"]);

/** 切換展示身分，其他查詢（帳號、待核准、庫存、排程…）全部重抓 */
export function useSwitchAccount() {
  const qc = useQueryClient();
  return async (accountId: string) => {
    await api.switchAccount(accountId);
    await qc.invalidateQueries({ predicate: (q) => !IDENTITY_FREE.has(String(q.queryKey[0])) });
  };
}

export const useApprovals = () =>
  useQuery({ queryKey: ["approvals"], queryFn: api.approvals, refetchInterval: 10_000 });

/** 區域解說草稿（docs/adr/029）：收錄中要等背景重建索引，短間隔重抓 */
export const useRegionDrafts = () =>
  useQuery({
    queryKey: ["region-drafts"],
    queryFn: api.regionDrafts,
    refetchInterval: (q) =>
      [...(q.state.data?.pending ?? []), ...(q.state.data?.mine ?? [])].some((d) => d.status === "indexing")
        ? 1500
        : 10_000,
  });

/** 目前身分看不看得到某個管理與診斷畫面（access.yaml 的 views；後端一樣會擋，這裡只是不發會 403 的請求） */
export function useCanView(view: "diagnostics" | "security_logs" | "audit") {
  const { data } = useAccounts();
  return !!data?.current.views?.includes(view);
}

export const useAudit = () => {
  const enabled = useCanView("audit");
  return useQuery({ queryKey: ["audit"], queryFn: api.audit, enabled });
};

/** 七段權限控管的拒絕並記錄（認證與授權、Jev Choice 擋下的請求、Jev Noul 剔除的洩密段落、輸出檢查） */
export const useSecurityLogs = (limit = 20) => {
  const enabled = useCanView("security_logs");
  return useQuery({
    queryKey: ["security-logs", limit],
    queryFn: () => api.securityLogs(limit),
    refetchInterval: 10_000,
    enabled,
  });
};

export const useRouteEvalRuns = () => useQuery({ queryKey: ["route-eval-runs"], queryFn: api.routeEvalRuns });

// ---- 照片建檔（docs/adr/013）
/** 收錄後背景重建索引（status＝indexing）期間每 1.5 秒重抓，直到 done／failed */
export const useIntakeDraft = (draftId: string | null) =>
  useQuery({
    queryKey: ["intake", draftId],
    queryFn: () => api.intakeDraft(draftId!),
    enabled: !!draftId,
    retry: false,
    refetchInterval: (q) => (q.state.data?.status === "indexing" ? 1500 : false),
  });
