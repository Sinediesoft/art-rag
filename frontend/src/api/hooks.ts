import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export const useArtworks = () => useQuery({ queryKey: ["artworks"], queryFn: api.listArtworks });

export const useArtwork = (id: string | undefined) =>
  useQuery({ queryKey: ["artwork", id], queryFn: () => api.getArtwork(id!), enabled: !!id });

export const useImageSearch = (imageId: string | null) =>
  useQuery({
    queryKey: ["search-image", imageId],
    queryFn: () => api.searchImage(imageId!),
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

export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 5000 });

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
