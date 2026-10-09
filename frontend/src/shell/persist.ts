import type { Domain } from "./design";
import { domainOf, questionOf, SCHEDULE_CUE, visiblePart } from "./outputs";
import { dispatchOf } from "./runner";
import { buildStages, gatewayStages, progressOf, summaryOf } from "./stages";
import type { ArchivedTurn, Conv, Turn } from "./types";
import type { EntryTheme } from "./theme";

/**
 * 存在這個瀏覽器的東西（localStorage）：對話紀錄、最後進過的模組、各模組正在看的成果、側欄收合、分隔線位置、入口主題。
 *
 * 只存「公開資料」：畫作問答的回答與公開段落、以文搜畫結果。不存：
 * - JWT（在 HttpOnly cookie，JavaScript 讀不到）、交接票 route_ticket、JWT payload、送給 Jev 的請求本文與代號對照
 * - 工廠內部資料（圖紙問答、Text-to-SQL 結果、圖紙查找、3D、排程、修改資料、內部／機密段落）：只留問句與當時的流程摘要，
 *   重新整理後要「以目前身分重新查詢」，由後端依當下的憑證重新判斷
 * - 被擋下、降級、閘道拒絕的請求：只留流程摘要（第幾段、規則、紀錄編號）
 * - 第 4 段剔除的段落（標題、主題都不留）、使用者原始輸入（只存後端遮蔽個資後的問句）、本機檔案（照片一律先上傳，只存伺服器的 image_id）
 */
export const STORAGE_KEY = "artrag-shell-v1";

export interface SavedTurn {
  id: string;
  text: string;
  imageId: string | null;
  forced: string | null;
  at: Turn["at"];
  ts: number;
  accountLabel: string | null;
  archived: ArchivedTurn;
}

export interface SavedConv {
  id: string;
  createdAt: number;
  updatedAt: number;
  module?: Domain;
  active: Partial<Record<Domain, string>>;
  turns: SavedTurn[];
}

export interface Saved {
  v: 1;
  convs: SavedConv[];
  collapsed: boolean;
  split: number;
  theme: EntryTheme;
}

export const DEFAULTS: Omit<Saved, "convs"> = { v: 1, collapsed: false, split: 50, theme: "dark" };

const PUBLIC = (level?: string) => !level || level === "公開";

/** 這一輪的內容是不是全部公開（可以存進瀏覽器、切換身分後也可以留在畫面上） */
export function isPublicTurn(t: Turn): boolean {
  if (t.archived) return true;
  const r = t.route;
  const p = t.part;
  if (!r || r.outcome !== "pass" || !p) return true;
  switch (p.kind) {
    case "route":
      return true;
    case "artSearch":
      return true;
    case "chat":
      return !!p.target.artwork_id && (p.sources?.sources ?? []).every((s) => PUBLIC(s.level));
    case "compare":
      return dispatchOf(r).compare?.kind === "artwork";
    default:
      return false;
  }
}

/** 一輪的存檔版本：見檔頭的規則 */
export function archive(t: Turn): ArchivedTurn {
  if (t.archived) return t.archived;
  const r = t.route;
  if (!r) {
    const f = t.failure;
    return {
      intentLabel: null,
      domain: null,
      outcome: f ? (f.status === 401 ? "gateway_denied" : "error") : "interrupted",
      stages: f?.status === 401 ? gatewayStages(f.code, "").map(({ key, short, state }) => ({ key, short, state })) : [],
      summary: f ? (f.status === 401 ? `閘道拒絕連線（401 ${f.code}）` : `沒有完成：${f.code}`) : "沒有完成",
      redacted: false,
    };
  }
  const mod = progressOf(t.part, t.phase);
  const p0 = t.part;
  // 寫入已經送出、沒有收到結果（送出中重新整理、連線中斷、身分改變）：伺服器可能已經寫好，記成「結果未確認」、不能重送
  const unconfirmed = r.outcome === "pass" && p0?.kind === "change" && (p0.status === "committing" || p0.status === "unconfirmed");
  // 第 6 段生成閘門降級、試算被拒絕：/agent/route 放行了，但這一輪一樣是「被關卡拒絕」
  const outcome = unconfirmed
    ? "unconfirmed"
    : r.outcome === "pass" && p0?.kind === "chat" && (p0.done?.degraded || p0.sources?.post_filter?.gate?.passed === false)
      ? "degraded"
      : r.outcome === "pass" && p0?.kind === "change" && p0.preview?.next === "rejected"
        ? "rejected"
        : r.outcome;
  const base: ArchivedTurn = {
    intentLabel: r.intent_label,
    domain: domainOf(t),
    outcome,
    stages: buildStages(r, mod).map(({ key, short, state }) => ({ key, short, state })),
    summary: unconfirmed ? writeUnconfirmed(p0?.kind === "change" ? p0.action : undefined) : summaryOf(r, mod),
    redacted: false,
  };
  if (outcome !== "pass") return { ...base, redacted: outcome !== "short_circuit" };
  if (!isPublicTurn(t)) return { ...base, redacted: true, refs: base.domain === "factory" ? factoryRefs(t) : undefined };
  const p = t.part;
  const d = dispatchOf(r);
  if (d.artwork_id) Object.assign(base, { artworkId: d.artwork_id, artworkLabel: d.artwork_label ?? d.artwork_id });
  else if (r.photo?.kind === "art" && r.photo.id) Object.assign(base, { artworkId: r.photo.id, artworkLabel: r.photo.label });
  if (p?.kind === "chat" && p.status === "done" && !p.done?.degraded) {
    base.answer = sanitizeForStorage(p.text, SECRET_ANSWER);
    base.sources = (p.sources?.sources ?? []).filter((s) => PUBLIC(s.level));
  }
  if (p?.kind === "artSearch" && p.status === "done") base.artResults = p.items.slice(0, 12);
  return base;
}

export const writeUnconfirmed = (action?: "commit" | "approval") =>
  `${action === "approval" ? "送主管核准" : "確認寫入"}已經送出、沒有收到結果：伺服器可能已經完成，請以目前身分到核准紀錄或庫存核對，不要直接重送`;

/** 工廠成果只留編號：圖紙 id（第 1 段確認看得到的，或圖紙查找第 1 名）、3D 工作編號、排程結果編號 */
function factoryRefs(t: Turn): ArchivedTurn["refs"] {
  const p = t.part;
  const refs: NonNullable<ArchivedTurn["refs"]> = {};
  const vis = visiblePart(t);
  if (vis) refs.partId = vis.id;
  else if (p?.kind === "partSearch" && p.status === "done" && p.items[0]) refs.partId = p.items[0].part.id;
  if (p?.kind === "sql" && p.result) refs.sql = true;
  if (p?.kind === "schedule" || (p?.kind === "sql" && SCHEDULE_CUE.test(questionOf(t)))) refs.schedule = true;
  if (p?.kind === "schedule" && p.job?.status === "done" && p.job.solution) refs.scheduleRunId = p.job.solution.run_id;
  if (p?.kind === "reconstruct" && p.job?.status === "done" && p.job.result?.ok && p.job.meta?.job_id) refs.cadJobId = p.job.meta.job_id;
  return Object.keys(refs).length ? refs : undefined;
}

const MASK = "［已遮蔽］";
/**
 * 帳密類關鍵字，原文與拆成單字後的文字（見 splitIdentifiers）各比對一次：
 * - 中文直接比對
 * - 不會出現在一般單字裡的（password、passwd、passphrase、passcode、credential、apikey、accesskey、secretkey、privatekey）
 *   出現在哪裡都算：dbpasswordhash、PASSWORDSALT、awsaccesskeyid
 * - 其餘英文關鍵字前面可以黏著前綴、後面要是字的結尾（可以加複數 s）：dbpassword、clientsecret、refreshtoken、userpwd 算，
 *   secretary、tokenizer 這類一般單字不算
 * - 有分隔的識別字（secret_key、secretKey、x-api-key）拆開後就是獨立的單字
 */
const SECRET_KEY = new RegExp(
  String.raw`密碼|口令|密鑰|私鑰|金鑰|帳密|憑證|權杖|驗證碼|通行碼|安全碼|password|passwd|passphrase|passcode|credential|apikey|accesskey|secretkey|privatekey|\b[\w-]*?(?:pwd|api[\s_-]?key|access[\s_-]?key|secret[\s_-]?key|secret|token|private[\s_-]?key|authorization|bearer|cookie|session[\s_-]?id)s?\b`,
  "i",
);

/**
 * 把程式識別字拆成單字：駝峰（secretKey → secret Key、APIKey → API Key）、底線、連字號、點都換成空白。
 * 只用在「有沒有提到帳密」的判斷，不改動要保存的文字
 */
const splitIdentifiers = (s: string) =>
  s
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .replace(/[_\-.]+/g, " ");
/** 不靠關鍵字也認得出來、自己有邊界的祕密：JWT、常見金鑰前綴、PEM 區塊、32 字以上的不透明字串 */
const SECRET_PATTERNS: [RegExp, string][] = [
  [/-----BEGIN [A-Z ]+-----[\s\S]*?(?:-----END [A-Z ]+-----|$)/g, MASK],
  [/\beyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?/g, MASK],
  [/\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}/g, MASK],
  [/\bAKIA[0-9A-Z]{16}\b/g, MASK],
  [/[A-Za-z0-9_\-+/=]{32,}/g, MASK],
];

/** 文字裡有沒有帳密類關鍵字（不管後面的值是單行、多行、引號、陣列、YAML 區塊） */
export const mentionsSecret = (s: string) => SECRET_KEY.test(s) || SECRET_KEY.test(splitIdentifiers(s));

/**
 * 存進瀏覽器前的帳密處理（後端的 mask_pii 只遮電話、Email、身分證）：
 * - 提到帳密類關鍵字的整段文字一律不保存，只存佔位文字——值的邊界（多行、YAML 區塊、JSON 陣列、跳脫引號…）
 *   不可能逐一解析完整，寧可整段不存，也不留下半個密碼
 * - 沒有關鍵字的文字，再把 JWT、金鑰前綴、PEM、長不透明字串遮掉
 * placeholder 是整段不存時用的佔位文字（問句或回答各自的說法）
 */
export function sanitizeForStorage(s: string, placeholder: string) {
  if (mentionsSecret(s)) return placeholder;
  return SECRET_PATTERNS.reduce((x, [re, to]) => x.replace(re, to), s);
}

export const SECRET_QUESTION = "（提到帳密、金鑰的提問，內容沒有保存）";
export const SECRET_ANSWER = "（回答提到帳密、金鑰，內容沒有保存）";

/** 被關卡拒絕的一輪：問句本身可能就是敏感內容（注入、套取帳密、看不到的文件），不存原文 */
export const REJECTED: Record<string, string> = {
  blocked_auth: "（第 1 段認證與授權擋下的提問，內容沒有保存）",
  blocked_guard: "（第 2 段 Jev Choice 擋下的提問，內容沒有保存）",
  degraded: "（降級回應「查無資料」的提問，內容沒有保存）",
  rejected: "（試算被拒絕的修改，內容沒有保存）",
  gateway_denied: "（閘道拒絕連線的提問，內容沒有保存）",
};

/** 從紀錄還原的一輪能不能重新查詢：被拒絕的、只剩佔位文字的不行（不能把佔位文字當問句送出） */
export function canRerun(t: Turn) {
  // 寫入送出中或結果未確認（還在畫面上、沒有重新整理）：重跑會清掉這一輪、重新試算，再按一次就可能是第二筆
  if (t.part?.kind === "change" && (t.part.status === "committing" || t.part.status === "unconfirmed")) return false;
  // 任何分頁送出過、還沒確認結果的寫入（獨立的標記，不會被其他分頁的舊快照蓋掉）
  if (hasMark(t.id)) return false;
  if (!t.archived) return true;
  if (t.archived.outcome && REJECTED[t.archived.outcome]) return false;
  // 結果未確認的寫入：重跑會重新試算、再按一次就可能變成第二筆
  if (t.archived.outcome === "unconfirmed") return false;
  return !!t.text && !/^（.*）$/.test(t.text);
}

function savedText(t: Turn, a: ArchivedTurn) {
  const placeholder = a.outcome ? REJECTED[a.outcome] : undefined;
  if (placeholder) return placeholder;
  if (!t.archived && !t.route) return "（沒有送達伺服器的提問）";
  // 有 route 才存（後端遮蔽個資後的問句）；從紀錄還原的那一輪 text 就是當時存的問句
  const q = t.archived ? t.text : t.route!.question;
  return q ? sanitizeForStorage(q, SECRET_QUESTION) : "（只有照片）";
}

function saveTurn(t: Turn): SavedTurn | null {
  // 還沒送達伺服器（或送出前就停止）的那一輪不存；閘道拒絕、連不上的只留流程摘要
  if (!t.archived && !t.route && !t.failure) return null;
  const archived = archive(t);
  return {
    id: t.id,
    text: savedText(t, archived),
    imageId: t.imageId,
    forced: t.forced,
    at: t.at,
    ts: t.ts,
    accountLabel: t.account?.label ?? null,
    archived,
  };
}

export function serialize(convs: Conv[], prefs: Omit<Saved, "v" | "convs">): Saved {
  return {
    ...DEFAULTS,
    ...prefs,
    convs: convs
      .map((c) => ({
        id: c.id,
        createdAt: c.createdAt,
        updatedAt: c.updatedAt,
        module: c.module,
        active: c.active,
        turns: c.turns.map(saveTurn).filter((x): x is SavedTurn => !!x),
      }))
      .filter((c) => c.turns.length > 0),
  };
}

/**
 * 送出過、還沒確認結果的寫入：獨立的 key，一般的對話快照存檔（400 ms 延遲、pagehide）不會碰它。
 * 對話快照是整份覆寫的，另一個分頁手上的舊快照（試算完成、可以重跑）存檔時會把「已送出」蓋掉；
 * 這份標記只有送出的那個分頁、在同一個身分下確實收到結果（完成，或伺服器明確拒絕）才清掉。
 * 刪除對話也不清：其他分頁手上的舊快照可能把那段對話存回來。
 * 只存輪次、對話與動作，不存 pending_id、試算內容、JWT 或交接票。
 *
 * 每一次送出一個 key（`artrag-shell-unconfirmed-v1:<輪次>:<送出代號>`）：新增只 setItem 自己這一次、解除只 removeItem
 * 自己這一次，不做「讀出整份、改完再整份寫回」，也不和別的分頁共用同一個 key——
 * 兩個分頁在讀與寫之間交錯時，共用的 key 會被後寫的一方覆寫，它解除時就把另一筆還沒確認的保護一起清掉。
 * 同一輪只要還有任何一份標記，就不能重跑、不能再送（hasMark 看的是整個輪次）。
 */
export const UNCONFIRMED_PREFIX = "artrag-shell-unconfirmed-v1:";
const turnPrefix = (turnId: string) => `${UNCONFIRMED_PREFIX}${turnId}:`;
/** 某一輪、某一次送出的 key；省略送出代號時是舊版（每輪一個 key）的格式，讀取時一樣算 */
export const markKey = (turnId: string, submission?: string) => (submission ? turnPrefix(turnId) + submission : UNCONFIRMED_PREFIX + turnId);
/** tab：送出的那個分頁（這個頁面載入時產生的隨機代號）；只有它能解除，其他分頁的提交清不掉 */
type Mark = { conv: string; action: "commit" | "approval"; ts: number; tab: string };
export type Marks = Record<string, Mark>;

/** 這個分頁（這一次載入頁面）的代號 */
export const TAB_ID = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
let submissions = 0;

/** 標記 key 是哪一輪的（key 的格式見上） */
const turnOfKey = (k: string) => k.slice(UNCONFIRMED_PREFIX.length).split(":")[0];

/** 列出某一輪（或全部）的標記 key */
function markKeys(turnId?: string): string[] {
  // 一次取得整份 key 清單再篩選，不用 length／key(i) 逐一讀：逐一讀的途中另一個分頁解除了前面的 key，
  // 後面的 key 會往前移一格而被跳過——「同一輪沒有別人的標記」就會誤判，兩份提交都送出
  let keys: string[];
  try {
    keys = Object.keys(localStorage);
  } catch {
    return []; // 讀不到瀏覽器儲存：這個分頁也寫不進標記，不會送出寫入
  }
  return keys.filter((k) => k.startsWith(UNCONFIRMED_PREFIX) && (turnId === undefined || turnOfKey(k) === turnId));
}

/** 標記的值壞掉也當成有標記（保守：不能重跑、誰都解除不了），動作不明就說「確認寫入」 */
function parseMark(raw: string): Mark {
  try {
    const m = JSON.parse(raw) as Partial<Mark> | null;
    if (m && typeof m === "object")
      return { conv: String(m.conv ?? ""), action: m.action === "approval" ? "approval" : "commit", ts: Number(m.ts) || 0, tab: String(m.tab ?? "") };
  } catch {
    /* 壞掉的值 */
  }
  return { conv: "", action: "commit", ts: 0, tab: "" };
}

/** 每一輪有沒有標記（同一輪有好幾份時取其中一份，只用來決定摘要的動作） */
export function readMarks(): Marks {
  const out: Marks = {};
  for (const k of markKeys())
    try {
      const raw = localStorage.getItem(k);
      if (raw !== null) out[turnOfKey(k)] ??= parseMark(raw);
    } catch {
      /* 讀不到就略過 */
    }
  return out;
}

/** 這一輪還有沒有任何一份標記（任何分頁、任何一次送出） */
export const hasMark = (turnId: string) => markKeys(turnId).length > 0;

/** 同一輪、自己這一次以外的標記 */
export const otherMarks = (turnId: string, mine: string) => markKeys(turnId).filter((k) => k !== mine);

/** 送出寫入之前呼叫：寫入自己這一次的標記，回傳它的 key；寫不進去回傳 null（呼叫端就不送） */
export function markUnconfirmed(turnId: string, convId: string, action: "commit" | "approval"): string | null {
  const key = markKey(turnId, `${TAB_ID}-${++submissions}`);
  try {
    localStorage.setItem(key, JSON.stringify({ conv: convId, action, ts: Date.now(), tab: TAB_ID } satisfies Mark));
    return key;
  } catch {
    return null;
  }
}

/**
 * 解除：只移除指定的那幾個 key，而且只限這個分頁自己寫的
 * （另一個分頁送出、還沒確認的標記，以及壞掉、沒有分頁代號的，都不會被這裡清掉）
 */
export function clearUnconfirmed(keys: string[]) {
  for (const k of keys)
    try {
      const raw = localStorage.getItem(k);
      if (raw !== null && parseMark(raw).tab === TAB_ID) localStorage.removeItem(k);
    } catch {
      /* 移除失敗：標記留著，保守 */
    }
}

/**
 * 有標記的那一輪一律當成結果未確認：不能重跑，摘要說明已送出。
 * - 從紀錄還原的（archived）：改 outcome 與摘要
 * - 還在畫面上、試算完成的卡片（另一個分頁對同一輪重新查詢過、還沒送出）：整張收起成同樣的摘要，不再給確認按鈕
 * - 這個分頁自己送出中／結果未確認、或已經有結果的卡片不動
 */
export function applyMarks(t: Turn, marks: Marks = readMarks()): Turn {
  const mk = marks[t.id];
  if (!mk) return t;
  const unconfirmed = (a: ArchivedTurn): ArchivedTurn => ({
    ...a,
    outcome: "unconfirmed",
    summary: writeUnconfirmed(mk.action),
    redacted: true,
    answer: undefined,
    sources: undefined,
    artResults: undefined,
    refs: undefined,
  });
  if (t.archived) return t.archived.outcome === "unconfirmed" ? t : { ...t, archived: unconfirmed(t.archived) };
  const p = t.part;
  if (p?.kind !== "change" || p.status === "committing" || p.status === "unconfirmed" || p.committed || p.approval) return t;
  return { ...t, text: t.route?.question || t.text, phase: "done", route: null, part: null, failure: null, archived: unconfirmed(archive({ ...t, archived: undefined })) };
}

/** 寫進瀏覽器；寫不進去（無痕模式、空間不足）回傳 false，內容只留在記憶體 */
export function save(s: Saved): boolean {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
    return true;
  } catch {
    return false;
  }
}

export function load(): { convs: Conv[]; prefs: Omit<Saved, "v" | "convs"> } {
  try {
    const s = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null") as Partial<Saved> | null;
    if (s?.v === 1 && Array.isArray(s.convs)) {
      const marks = readMarks();
      const convs: Conv[] = s.convs.map((c) => ({
        id: c.id,
        createdAt: c.createdAt,
        updatedAt: c.updatedAt,
        module: c.module,
        active: c.active ?? {},
        notices: [],
        turns: c.turns.map((t) =>
          // 快照可能被其他分頁的舊版本蓋過：以獨立的「已送出」標記為準
          applyMarks(
            {
              id: t.id,
              text: t.text,
              imageId: t.imageId,
              forced: t.forced,
              at: t.at,
              ts: t.ts,
              account: t.accountLabel ? { id: "", label: t.accountLabel } : null,
              phase: "done",
              route: null,
              failure: null,
              part: null,
              archived: t.archived,
            },
            marks,
          ),
        ),
      }));
      return {
        convs,
        prefs: {
          collapsed: !!s.collapsed,
          split: typeof s.split === "number" ? Math.min(70, Math.max(30, s.split)) : 50,
          theme: s.theme === "light" ? "light" : "dark",
        },
      };
    }
  } catch {
    /* 壞掉的紀錄：當作沒有 */
  }
  return { convs: [], prefs: { collapsed: DEFAULTS.collapsed, split: DEFAULTS.split, theme: DEFAULTS.theme } };
}

/** 切換身分後：其他身分問到的非公開內容從畫面上收起來（只留問句與流程摘要），要看就以目前身分重新查詢 */
export function redactForeign(t: Turn, accountId: string): Turn {
  if (t.archived || !t.account || t.account.id === accountId) return t;
  return redactPrivate(t);
}

/**
 * 不管是哪個身分問的：非公開內容（工廠資料、內部段落、SQL 結果、3D、排程、試算）一律收起成流程摘要與編號。
 * 憑證更新、身分還沒重新確認時用：這段期間誰都不能看舊身分已經拿到的內容，已完成、已停止、出錯的也一樣
 */
export function redactPrivate(t: Turn): Turn {
  if (t.archived || isPublicTurn(t)) return t;
  return { ...t, text: t.route?.question || t.text, route: null, part: null, failure: null, archived: archive(t) };
}

/**
 * 切換身分時還在跑的一輪（第 1～7 段或 3D／排程任務）：請求用的是舊身分的 JWT，
 * store 會中止請求並讓舊的 callback 失效，這裡把已經收到的內容收起來（只留問句與流程摘要）。
 */
export function interruptForAccount(t: Turn): Turn {
  const a = archive({ ...t, archived: undefined });
  const hide = !isPublicTurn(t) || !t.route;
  return {
    ...t,
    // 還沒經過後端遮蔽個資的原始輸入不留
    text: t.route ? t.route.question || "（只有照片）" : "（切換身分時中止的提問）",
    phase: "stopped",
    route: null,
    part: null,
    failure: null,
    archived: {
      ...a,
      outcome: a.outcome === "pass" ? "interrupted" : a.outcome,
      // 已經送出的寫入不是「中止」：伺服器可能已經完成（見 archive 的 unconfirmed）
      summary: a.outcome === "unconfirmed" ? a.summary : "切換身分，已中止這一輪",
      redacted: hide || a.redacted,
      answer: undefined,
      sources: undefined,
      artResults: undefined,
    },
  };
}

/** 紀錄清單上的標題：第一句問題 */
export const titleOf = (c: Conv) => (c.turns[0] ? c.turns[0].route?.question || c.turns[0].text || "（只有照片）" : "新對話");

export function whenOf(ts: number) {
  const d = new Date(ts);
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (ts >= start) return "今天";
  if (ts >= start - 86400_000) return "昨天";
  if (ts >= start - 7 * 86400_000) return "這週";
  return `${d.getMonth() + 1} 月`;
}

export function timeOf(ts: number) {
  const d = new Date(ts);
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  return ts >= start ? `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` : `${d.getMonth() + 1}/${d.getDate()}`;
}
