import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { InventoryOverviewRow } from "../api/client";
import { useHealth, useInventoryOverview, useInventorySchema } from "../api/hooks";
import { ApiErrorMessage, Loading } from "../components/common/Feedback";
import { SqlAnswer } from "../components/inventory/SqlAnswer";

const SUGGESTIONS = [
  "哪些零件的可用庫存低於安全庫存？",
  "哪些訂單的未出貨數量超過目前可用庫存？",
  "目前有哪些不良品？原因是什麼？",
  "二廠成品倉放了哪些零件？各多少件？",
  "10 月 10 日前到期、還沒出完貨的訂單有哪些？",
  "九月每個零件各出貨了幾件？",
  "目前庫存依標準成本計算總價值多少？",
  "VMC-01 還有哪些工單沒完工？",
];

const STEPS = [
  { title: "產生 SQL", body: "本地 Qwen3-VL 讀資料表結構、欄位值與範例，把中文問題寫成一條 SQLite 查詢" },
  {
    title: "安全執行",
    body: "只准一條 SELECT → 唯讀連線＋資料表／函式白名單 → 2 秒逾時；執行失敗就把錯誤訊息回饋給模型重寫",
  },
  { title: "依結果回答", body: "模型只看查詢結果回答，查無資料就直說；SQL 與結果表都攤開給人檢查" },
];

interface Turn {
  id: number;
  question: string;
}

export function InventoryPage() {
  const [params, setParams] = useSearchParams();
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const { data: health } = useHealth();
  const hybrid = health?.strategies.hybrid;

  const ask = (question: string) => {
    const q = question.trim();
    if (!q) return;
    setTurns((t) => [{ id: Date.now(), question: q }, ...t]);
    setInput("");
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    ask(input);
  };

  // 從圖紙頁「問庫存」帶問題過來：/inventory?q=…（ref 防止 StrictMode 重跑 effect 時問兩次）
  const handledQuery = useRef(false);
  useEffect(() => {
    const q = params.get("q");
    if (q && !handledQuery.current) {
      handledQuery.current = true;
      ask(q);
      setParams({}, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex flex-col gap-8">
      <section className="card p-6 sm:p-10">
        <p className="t-eyebrow">工廠庫存 · Text-to-SQL · 地端</p>
        <h1 className="t-display mt-2">
          用中文問庫存，
          <br className="sm:hidden" />
          系統自己寫 SQL
        </h1>
        <p className="t-body mt-3 max-w-2xl text-ink-80">
          六張圖紙的庫存、工單與客戶訂單存在本機的關聯式資料庫。問題由本地模型轉成 SQL、在唯讀連線上執行，
          再依查詢結果回答——製程文件走 RAG，<b className="font-semibold text-ink">數字與明細走資料庫</b>，資料全程不出本機。
        </p>

        <form onSubmit={submit} className="mt-6 flex max-w-2xl gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="例如「連接法蘭還有幾件可以出貨？」"
            maxLength={300}
            className="field min-w-0 flex-1"
          />
          <button
            className="btn-primary shrink-0"
            disabled={!input.trim()}
          >
            查詢
          </button>
        </form>
        <div className="mt-3 flex flex-wrap gap-2">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => ask(s)}
              className="chip text-ink-80 hover:border-accent hover:text-accent"
            >
              {s}
            </button>
          ))}
        </div>
      </section>

      {hybrid && !hybrid.available && (
        <div className="rounded-xl border border-warning/30 bg-warning-soft/60 p-3 text-sm text-warning">
          本地推論伺服器目前無法使用（{hybrid.detail}），查詢會改走本地備援模型；都失敗時服務暫停，不改走雲端。
        </div>
      )}

      {turns.length > 0 && (
        <section className="flex flex-col gap-5">
          {turns.map((t) => (
            <article key={t.id} className="flex flex-col gap-2">
              <div className="self-end rounded-2xl rounded-br-sm bg-ink px-4 py-2 text-parchment">{t.question}</div>
              <div className="rounded-2xl rounded-bl-sm border border-hairline bg-card p-4">
                <SqlAnswer question={t.question} />
              </div>
            </article>
          ))}
        </section>
      )}

      <section className="grid gap-3 sm:grid-cols-3">
        {STEPS.map((s, i) => (
          <div key={s.title} className="card p-5">
            <p className="font-mono text-xs font-semibold text-ink-48">0{i + 1}</p>
            <p className="font-semibold">{s.title}</p>
            <p className="mt-1 text-sm text-ink-80">{s.body}</p>
          </div>
        ))}
      </section>

      <OverviewTable />
      <SchemaExplorer />
    </div>
  );
}

function OverviewTable() {
  const { data, isLoading, error } = useInventoryOverview();
  if (isLoading) return <Loading />;
  if (error || !data)
    return <ApiErrorMessage error={error} />;
  const cols: [keyof InventoryOverviewRow, string][] = [
    ["available", "可用"],
    ["reserved", "保留"],
    ["inspecting", "待檢"],
    ["defective", "不良"],
    ["safety_stock", "安全庫存"],
    ["open_demand", "未出貨需求"],
    ["in_production", "生產中"],
  ];
  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="t-tagline">{data.items.length} 張圖紙的庫存</h2>
        <span className="text-xs text-ink-48">
          資料日期 {data.as_of} · {data.company}
        </span>
      </div>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[640px] text-sm">
          <thead className="t-caption-strong whitespace-nowrap text-left text-ink-48">
            <tr>
              <th className="px-4 py-2.5 font-semibold">品名</th>
              {cols.map(([, label]) => (
                <th key={label} className="px-3 py-2.5 text-right font-semibold">
                  {label}
                </th>
              ))}
              <th className="px-4 py-2.5 font-semibold">狀態</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((r) => {
              const low = r.safety_stock != null && r.available < r.safety_stock;
              const short = r.open_demand > r.available;
              return (
                <tr key={r.part_id} className="border-t border-hairline">
                  <td className="px-4 py-2.5">
                    <Link to={`/drawings/${r.part_id}`} className="hover:text-accent hover:underline">
                      {r.name}
                    </Link>
                    <span className="ml-1.5 whitespace-nowrap font-mono text-xs text-ink-48">{r.part_no}</span>
                  </td>
                  {cols.map(([k]) => (
                    <td
                      key={k}
                      className={`px-3 py-2.5 text-right tabular-nums ${
                        k === "available" && low ? "font-semibold text-danger" : ""
                      } ${k === "defective" && r.defective ? "text-warning" : ""}`}
                    >
                      {(r[k] as number | null)?.toLocaleString() ?? "—"}
                    </td>
                  ))}
                  <td className="px-4 py-2.5">
                    <div className="flex flex-wrap gap-1">
                      {low && (
                        <span className="rounded-full bg-danger-soft px-2 py-0.5 text-xs font-semibold text-danger">
                          低於安全庫存
                        </span>
                      )}
                      {short && (
                        <span className="rounded-full bg-warning-soft px-2 py-0.5 text-xs font-semibold text-warning">
                          需求 &gt; 可用
                        </span>
                      )}
                      {!low && !short && <span className="text-xs text-success">正常</span>}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-ink-48">
        這張表是固定查詢（不經模型）。「需求 &gt; 可用」：未出貨訂單的總需求大於可用庫存，需要趕工或調撥。
      </p>
    </section>
  );
}

function SchemaExplorer() {
  const { data } = useInventorySchema();
  if (!data) return null;
  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="t-tagline">資料庫結構</h2>
        <span className="text-xs text-ink-48">模型看到的就是這份 schema（prompt {data.prompt_version}）</span>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {data.tables.map((t) => (
          <details key={t.name} className="rounded-lg border border-hairline bg-card px-4 py-2.5">
            <summary className="cursor-pointer">
              <span className="font-mono text-sm font-semibold text-ink">{t.name}</span>
              <span className="ml-2 text-sm text-ink-80">{t.description}</span>
              <span className="ml-2 text-xs text-ink-48">{t.rows != null ? `${t.rows} 筆` : "檢視表"}</span>
            </summary>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
              {t.columns.map((c) => (
                <div key={c.name} className="contents">
                  <dt className="font-mono text-ink">
                    {c.name} <span className="text-ink-48">{c.type}</span>
                  </dt>
                  <dd className="text-ink-80">{c.description}</dd>
                </div>
              ))}
            </dl>
          </details>
        ))}
      </div>
      <p className="mt-2 text-xs text-ink-48">
        示範資料：虛構工廠「示範精密機械」，客戶、單號與數量皆為虛構。資料來源為 <code className="font-mono">kb/inventory/</code>{" "}
        的 JSON（一張圖紙一個檔），改了會自動重建資料庫；模型產生的 SQL 只能讀取上列資料表。
      </p>
    </section>
  );
}
