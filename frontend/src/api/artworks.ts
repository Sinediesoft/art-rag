// 畫作圖紙：公開畫作清單、辨識、搜尋、色彩、畫作卡推測、影像對位與藝術家區域註記（問答在 sse.ts 的 streamChat）
import { json, request } from "./http";
import type {
  ArtworkDetail,
  ColorAnalysis,
  ImageAlignment,
  ImageSearchResponse,
  RegionDraft,
  RegionDraftRequest,
  RegionDraftsResponse,
  Schemas,
  StyleGuess,
  TextSearchResponse,
} from "./types";
import { trackWrite } from "./writes";

export const artApi = {
  searchImage: (imageId: string) => request<ImageSearchResponse>("/search/image", json({ image_id: imageId })),
  searchText: (q: string) => request<TextSearchResponse>(`/search/text?q=${encodeURIComponent(q)}`),
  listArtworks: () => request<Schemas["ArtworkListResponse"]>("/artworks"),
  getArtwork: (id: string) => request<ArtworkDetail>(`/artworks/${encodeURIComponent(id)}`),
  /** 色彩分析（docs/adr/010）：知識庫畫作讀索引算好的結果，上傳照片即時計算 */
  artworkColors: (id: string) => request<ColorAnalysis>(`/artworks/${encodeURIComponent(id)}/colors`),
  photoColors: (imageId: string) => request<ColorAnalysis>(`/images/${encodeURIComponent(imageId)}/colors`),
  /** 畫作卡推測（docs/adr/018）：知識庫沒有這幅畫時，推測風格大類、題材、媒材（沒有出處） */
  photoStyle: (imageId: string) => request<StyleGuess>(`/images/${encodeURIComponent(imageId)}/style`),
  /** 影像對位與比對（docs/adr/012）：target 是 artwork:<畫作 id> 或 part:<圖紙 id> */
  photoAlignment: (imageId: string, target: string) =>
    request<ImageAlignment>(`/images/${encodeURIComponent(imageId)}/align?target=${encodeURIComponent(target)}`),
  // 畫面區域（docs/adr/029）：藝術家在畫上圈一塊、寫解說送草稿，主管收錄或退回
  regionDrafts: () => request<RegionDraftsResponse>("/region-drafts"),
  createRegionDraft: (artworkId: string, body: RegionDraftRequest) =>
    trackWrite("送出區域解說", () => request<RegionDraft>(`/artworks/${encodeURIComponent(artworkId)}/region-drafts`, json(body)), `region:create:${artworkId}`),
  commitRegionDraft: (draftId: string) =>
    trackWrite("收錄區域解說", () => request<RegionDraft>(`/region-drafts/${encodeURIComponent(draftId)}/commit`, { method: "POST" }), `region:${draftId}`),
  returnRegionDraft: (draftId: string, reason: string) =>
    trackWrite("退回區域解說", () => request<RegionDraft>(`/region-drafts/${encodeURIComponent(draftId)}/return`, json({ reason })), `region:${draftId}`),
  withdrawRegionDraft: (draftId: string) =>
    trackWrite("撤回區域解說", () => request<Schemas["OkResponse"]>(`/region-drafts/${encodeURIComponent(draftId)}`, { method: "DELETE" }), `region:${draftId}`),
};
