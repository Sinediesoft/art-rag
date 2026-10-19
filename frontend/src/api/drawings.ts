// 工廠圖紙：授權內的清單、單筆、以圖／以文找圖紙（問答在 sse.ts 的 streamChat，與畫作共用七段管線）
import { json, request } from "./http";
import type { DrawingSearchResponse, PartDetail, PartTextSearchResponse, Schemas } from "./types";

export const drawingsApi = {
  listParts: () => request<Schemas["PartListResponse"]>("/parts"),
  getPart: (id: string) => request<PartDetail>(`/parts/${encodeURIComponent(id)}`),
  searchDrawing: (imageId: string) => request<DrawingSearchResponse>("/search/drawing", json({ image_id: imageId })),
  searchParts: (q: string) => request<PartTextSearchResponse>(`/search/parts?q=${encodeURIComponent(q)}`),
};
