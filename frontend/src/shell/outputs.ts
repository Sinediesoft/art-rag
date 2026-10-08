import type { ArtworkSummary } from "../api/client";
import type { Domain } from "./design";
import { dispatchOf } from "./runner";
import type { CadJob, Conv, SolveJob, SqlPart, Turn } from "./types";

/**
 * 入口與模組的分工（來源：ArtRAG-前端demo/source/src/modules.ts）：
 * - 入口只回「簡介」，並依七段流程分派的結果給一顆進入模組的按鈕
 * - 模組左邊是對話，右邊把對話產生的成果大張展示
 * 成果全部由真實回應推出：圖紙＝第 1 段確認看得到的那一張或圖紙查找的結果、3D＝Ortho2CAD 的 STL、
 * 庫存／查詢＝Text-to-SQL 的結果表、排程＝/production/overview 與 Timefold 的解；畫作同理。
 */
const FACTORY = ["drawing_search", "drawing_qa", "data_query", "reconstruct", "schedule", "modify"];
const ART = ["art_search", "art_qa"];

/**
 * 這一輪屬於哪個模組；被擋下、降級、要你選、超出範圍、出錯的都不屬於任何模組（不給跳轉）。
 * 第 6 段生成閘門降級「查無資料」也算：/agent/route 放行了，但權限內沒有可答的內容，不能反過來給模組按鈕。
 */
export function domainOf(t: Turn): Domain | null {
  if (t.archived) return t.archived.domain;
  const r = t.route;
  if (!r || r.outcome !== "pass" || r.gate === "clarify" || r.gate === "out_of_scope") return null;
  if (r.photo?.kind === "unknown" && !r.question) return null;
  if (t.phase === "error") return null;
  const p = t.part;
  if (p?.kind === "chat" && (p.done?.degraded || p.sources?.post_filter?.gate?.passed === false || p.error)) return null;
  if (r.intent === "compare") return dispatchOf(r).compare?.kind === "part" ? "factory" : dispatchOf(r).compare?.kind === "artwork" ? "art" : null;
  if (FACTORY.includes(r.intent)) return "factory";
  if (ART.includes(r.intent)) return "art";
  return null;
}

/** 這一輪問的是畫作哪一面（只決定右側用哪種檢視；內容一律來自後端） */
const DETAIL_CUE = /技法|筆觸|筆法|皴|點描|厚塗|構圖|畫面|細看|細節|局部|畫了什麼|重點|描繪|顏色|色彩/;
const TIMELINE_CUE = /年表|生平|時代|當時|在哪|何時|哪一年|背景|創作|版本|展出|多久|畫家|作者|誰畫/;
const SIMILAR_CUE = /相近|相似|類似|風格接近|像這幅|同風格/;
export const SCHEDULE_CUE =/排程|甘特|工單|產線|機台/;

export type OutputKind = "drawing" | "stock" | "query" | "model" | "schedule" | "artwork" | "detail" | "timeline" | "similar";

export const KIND_LABEL: Record<OutputKind, string> = {
  drawing: "圖紙",
  stock: "庫存",
  query: "查詢",
  model: "3D",
  schedule: "排程",
  artwork: "作品",
  detail: "細看",
  timeline: "年表",
  similar: "相似作品",
};

export interface Output {
  key: string;
  kind: OutputKind;
  domain: Domain;
  turnId: string;
  title: string;
  meta: string;
  partId?: string;
  artworkId?: string;
  sql?: SqlPart;
  job?: CadJob;
  /** 排程：null＝目前的排程（/production/overview）；有值＝這一輪求解的結果 */
  solve?: SolveJob | null;
  /** 相似作品：以文搜畫的結果；search＝使用者的搜尋句，沒有就是「與〈作品〉相近」 */
  items?: { artwork: ArtworkSummary; score: number }[];
  search?: string;
  focus?: "技法" | "畫面";
  /** 從紀錄還原的工廠成果：只有編號，展示區以目前的 JWT 重新讀取（3D 讀 /cad/jobs、排程讀 /schedule/runs） */
  cadJobId?: string;
  runId?: string;
  /** 查詢結果沒有存進瀏覽器（工廠內部資料），要以目前身分重新查詢 */
  stale?: boolean;
}

/** 畫作是公開資料：問答在第 6 段降級「查無資料」時不給跳轉，但模組裡仍可以看這幅畫與相似作品（來自以文搜畫，不靠生成） */
function publicArt(t: Turn) {
  const p = t.part;
  return t.route?.outcome === "pass" && p?.kind === "chat" && !!p.target.artwork_id && !p.error && (!!p.done?.degraded || p.sources?.post_filter?.gate?.passed === false);
}

/** 這一輪的問句（已遮蔽個資） */
export const questionOf = (t: Turn) => t.route?.question || t.text;

const thumbOfArtwork = (id: string) => `/api/v1/artworks/${encodeURIComponent(id)}/image?size=thumb`;
const thumbOfPart = (id: string) => `/api/v1/parts/${encodeURIComponent(id)}/drawing?size=thumb`;
export const thumbOf = (o: Output) =>
  o.partId ? thumbOfPart(o.partId) : o.artworkId ? thumbOfArtwork(o.artworkId) : o.items?.[0] ? o.items[0].artwork.thumb_url : null;

/** 第 1 段確認看得到的圖紙（auth.filter 只有看得到時才回 doc_level）：看不到的不能出現在任何按鈕或縮圖上 */
export function visiblePart(t: Turn): { id: string; label: string; level: string } | null {
  const r = t.route;
  if (!r || r.outcome !== "pass") return null;
  const d = dispatchOf(r);
  const f = r.auth.filter;
  if (!d.part_id || f?.doc_id !== d.part_id || !f.doc_level) return null;
  return { id: d.part_id, label: d.part_label ?? f.doc_label ?? d.part_id, level: f.doc_level };
}

export function artworkOf(t: Turn): { id: string; label: string } | null {
  if (t.archived) return t.archived.artworkId ? { id: t.archived.artworkId, label: t.archived.artworkLabel ?? t.archived.artworkId } : null;
  const r = t.route;
  if (!r || r.outcome !== "pass") return null;
  const d = dispatchOf(r);
  if (d.artwork_id) return { id: d.artwork_id, label: d.artwork_label ?? d.artwork_id };
  if (r.photo?.kind === "art" && r.photo.id) return { id: r.photo.id, label: r.photo.label };
  return null;
}

const sqlKind = (p: SqlPart): OutputKind => {
  const cols = p.result?.columns.join(" ") ?? "";
  return /可用|庫存|數量|在庫|qty|stock/i.test(cols) ? "stock" : "query";
};

/** 依對話推出所有成果（時間順序），以及每一輪產生了哪些 */
export function deriveOutputs(conv: Conv | null) {
  const list: Output[] = [];
  const byTurn: Record<string, string[]> = {};
  const add = (o: Output) => {
    if (!list.some((x) => x.key === o.key)) list.push(o);
    const keys = (byTurn[o.turnId] ??= []);
    if (!keys.includes(o.key)) keys.push(o.key);
  };
  for (const t of conv?.turns ?? []) {
    const d = domainOf(t) ?? (publicArt(t) ? "art" : null);
    if (!d) continue;
    const q = questionOf(t);
    const refs = t.archived?.refs;
    if (d === "factory" && refs) {
      const code = (id: string) => id.toUpperCase();
      if (refs.partId) add({ key: `drawing:${refs.partId}`, kind: "drawing", domain: d, turnId: t.id, partId: refs.partId, title: `圖紙 ${code(refs.partId)}`, meta: "從紀錄還原・以目前身分讀取" });
      if (refs.sql) add({ key: `sql:${t.id}`, kind: "query", domain: d, turnId: t.id, stale: true, title: q, meta: "查詢結果沒有存進瀏覽器" });
      if (refs.schedule) add({ key: `schedule:now:${t.id}`, kind: "schedule", domain: d, turnId: t.id, solve: null, title: "目前的生產排程", meta: "生產排程・現行結果" });
      if (refs.scheduleRunId) add({ key: `schedule:run:${t.id}`, kind: "schedule", domain: d, turnId: t.id, runId: refs.scheduleRunId, title: "重新排程結果", meta: refs.scheduleRunId });
      if (refs.cadJobId) add({ key: `model:${t.id}`, kind: "model", domain: d, turnId: t.id, partId: refs.partId, cadJobId: refs.cadJobId, title: "3D 模型", meta: "Ortho2CAD・從紀錄還原" });
      continue;
    }
    if (d === "factory") {
      const part = t.part;
      const vis = visiblePart(t);
      if (vis) add({ key: `drawing:${vis.id}`, kind: "drawing", domain: d, turnId: t.id, partId: vis.id, title: vis.label, meta: `圖紙・${vis.level}` });
      if (part?.kind === "partSearch" && part.status === "done" && part.items[0] && !vis) {
        const p = part.items[0].part;
        add({ key: `drawing:${p.id}`, kind: "drawing", domain: d, turnId: t.id, partId: p.id, title: p.name_zh, meta: `圖紙查找第 1 名・${p.confidentiality}` });
      }
      if (part?.kind === "sql" && part.result)
        add({
          key: `sql:${t.id}`,
          kind: sqlKind(part),
          domain: d,
          turnId: t.id,
          sql: part,
          title: part.question,
          meta: `${part.result.row_count} 筆・唯讀查詢`,
        });
      if ((part?.kind === "sql" && SCHEDULE_CUE.test(q)) || part?.kind === "schedule")
        add({ key: `schedule:now:${t.id}`, kind: "schedule", domain: d, turnId: t.id, solve: null, title: "目前的生產排程", meta: "生產排程・現行結果" });
      if (part?.kind === "schedule" && part.job?.status === "done" && part.job.solution)
        add({
          key: `schedule:run:${t.id}`,
          kind: "schedule",
          domain: d,
          turnId: t.id,
          solve: part.job,
          title: "重新排程結果",
          meta: `${part.job.solution.work_orders.length} 張工單・${part.job.solution.run_id}`,
        });
      if (part?.kind === "reconstruct" && part.job?.status === "done" && part.job.result?.ok && part.job.result.files["model.stl"])
        add({
          key: `model:${t.id}`,
          kind: "model",
          domain: d,
          turnId: t.id,
          partId: part.partId ?? undefined,
          job: part.job,
          title: `${part.partLabel ?? "圖紙"} 3D 模型`,
          meta: `Ortho2CAD・${part.job.done?.model ?? ""}・全程在本機`,
        });
    } else {
      const a = artworkOf(t);
      const part = t.part;
      const results = part?.kind === "artSearch" && part.status === "done" ? part.items : t.archived?.artResults;
      if (a) {
        add({ key: `artwork:${a.id}`, kind: "artwork", domain: d, turnId: t.id, artworkId: a.id, title: a.label, meta: "作品・知識庫" });
        if (SIMILAR_CUE.test(q))
          add({ key: `similar:${t.id}`, kind: "similar", domain: d, turnId: t.id, artworkId: a.id, title: `與〈${a.label}〉相近的作品`, meta: "以文搜畫・依風格標籤" });
        else if (DETAIL_CUE.test(q))
          add({
            key: `detail:${a.id}`,
            kind: "detail",
            domain: d,
            turnId: t.id,
            artworkId: a.id,
            focus: /技法|筆|皴|點描|厚塗|顏色|色彩/.test(q) ? "技法" : "畫面",
            title: `細看〈${a.label}〉`,
            meta: "段落與色彩分析・可縮放",
          });
        else if (TIMELINE_CUE.test(q))
          add({ key: `timeline:${a.id}`, kind: "timeline", domain: d, turnId: t.id, artworkId: a.id, title: `〈${a.label}〉年表`, meta: "館藏作品依年代排列" });
      }
      if (results?.length)
        add({
          key: `similar:${t.id}`,
          kind: "similar",
          domain: d,
          turnId: t.id,
          items: results,
          search: q,
          title: `「${q}」的搜尋結果`,
          meta: `${results.length} 幅・Chinese-CLIP＋bge-m3`,
        });
    }
  }
  return { list, byTurn };
}

/** 跳轉按鈕上的一句話：進去之後會看到什麼；沒有分派到模組、被擋下、試算被拒絕時不給 */
export function ctaOf(t: Turn): { domain: Domain; line: string } | null {
  const d = domainOf(t);
  if (!d || t.phase === "routing" || t.phase === "error") return null;
  const p = t.part;
  if (p?.kind === "change" && p.preview?.next === "rejected") return null;
  if (d === "factory") {
    const vis = visiblePart(t);
    const intent = t.route?.intent;
    const line =
      intent === "reconstruct"
        ? vis
          ? `開始把〈${vis.label}〉轉成 3D，模型大張顯示`
          : "選一張圖紙轉成 3D"
        : intent === "schedule"
          ? "確認重新排程，甘特圖大張顯示"
          : intent === "modify"
            ? "核對修改前後的數字，確認後寫入"
            : p?.kind === "sql"
              ? SCHEDULE_CUE.test(questionOf(t))
                ? "大張檢視生產排程甘特圖與查詢結果"
                : "大張檢視完整的查詢結果"
              : vis
                ? `大圖檢視〈${vis.label}〉的圖紙`
                : "大圖檢視圖紙";
    return { domain: d, line };
  }
  const a = artworkOf(t);
  const q = questionOf(t);
  const label = a?.label ?? "";
  const line = !a
    ? "在展牆上看搜尋到的作品"
    : SIMILAR_CUE.test(q)
      ? `並排比較〈${label}〉與相近的作品`
      : DETAIL_CUE.test(q)
        ? `細看〈${label}〉的畫面與技法`
        : TIMELINE_CUE.test(q)
          ? `查看〈${label}〉的創作年表`
          : `在展牆上細看〈${label}〉`;
  return { domain: d, line };
}

export interface Subject {
  kind: "part" | "artwork";
  id: string;
  label: string;
  meta: string;
}

/** 模組左上的對象：正在看的成果所屬的零件或畫作；沒有就用這段對話最後談到的 */
export function subjectFor(domain: Domain, active: Output | undefined, conv: Conv | null): Subject | null {
  const turns = conv?.turns ?? [];
  const from = active ? turns.find((t) => t.id === active.turnId) : undefined;
  if (active?.partId) {
    const p = from?.part;
    const label =
      (from && visiblePart(from)?.label) ??
      (p?.kind === "partSearch" ? p.items.find((x) => x.part.id === active.partId)?.part.name_zh : undefined) ??
      (p?.kind === "reconstruct" ? p.partLabel : undefined) ??
      active.partId.toUpperCase();
    return { kind: "part", id: active.partId, label, meta: active.meta };
  }
  if (active?.artworkId) {
    const label = (from && artworkOf(from)?.label) ?? active.artworkId;
    return { kind: "artwork", id: active.artworkId, label, meta: active.meta };
  }
  for (let i = turns.length - 1; i >= 0; i--) {
    const t = turns[i];
    if (domain === "factory") {
      const v = visiblePart(t);
      if (v) return { kind: "part", id: v.id, label: v.label, meta: `圖紙・${v.level}` };
    } else {
      const a = artworkOf(t);
      if (a) return { kind: "artwork", id: a.id, label: a.label, meta: "作品・知識庫" };
    }
  }
  return null;
}

/** 模組裡說「這幅畫／這張圖」時，把正在看的對象帶進問句（/agent/route 一句一句判斷，沒有上下文） */
const DEICTIC = /這(幅|張|件|個|份|零件|圖|畫)|此(畫|圖|件)|它|該(畫|圖|件)/;
export function withSubject(text: string, subject: Subject | null) {
  if (!subject || !text || text.includes(subject.label) || !DEICTIC.test(text)) return text;
  return `〈${subject.label}〉${text}`;
}
