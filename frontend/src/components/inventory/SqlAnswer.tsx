import { useEffect, useRef } from "react";
import type { SqlResultEvent } from "../../api/sse";
import { cleanSqlDraft, useSqlAsk } from "../../hooks/useSqlAsk";
import { seconds } from "../../lib/format";
import { ErrorMessage } from "../common/Feedback";
import { EgressBadge } from "../common/StatusNotices";

function Spinner() {
  return <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-hairline border-t-accent" />;
}

const STATUS_TEXT = {
  generating: "本地模型依資料表結構撰寫 SQL…",
  executing: "安全檢查並執行查詢…",
  answering: "依查詢結果整理回答…",
} as const;

/** 一次 Text-to-SQL：串流 SQL → 修正紀錄 → 查詢結果表 → 文字回答 → 延遲與外送標示 */
export function SqlAnswer({ question }: { question: string }) {
  const { state, start } = useSqlAsk();
  const codeRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    start({ question });
  }, [question, start]);

  const { status, draft, attempts, result, answer, done, error, attempt } = state;
  const last = attempts[attempts.length - 1];
  const generating = status === "generating";
  const sql = generating ? cleanSqlDraft(draft) : (last?.sql ?? cleanSqlDraft(draft));
  const rejected = error?.code === "SQL_REJECTED";
  // 被拒絕的那條 SQL 直接顯示在主程式碼區，不放進「修正紀錄」
  const failed = attempts.filter((a) => !a.ok && !(rejected && a === last));

  useEffect(() => {
    if (generating && codeRef.current) codeRef.current.scrollTop = codeRef.current.scrollHeight;
  }, [draft, generating]);

  return (
    <div className="flex flex-col gap-3">
      {status in STATUS_TEXT && (
        <p className="flex items-center gap-2 text-sm text-accent">
          <Spinner />
          {STATUS_TEXT[status as keyof typeof STATUS_TEXT]}
          {attempt > 1 && generating && <span className="text-warning">（第 {attempt} 次：依錯誤訊息修正）</span>}
        </p>
      )}

      {failed.map((a) => (
        <details key={a.attempt} className="rounded-lg border border-warning/30 bg-warning-soft/50 px-3 py-2 text-sm">
          <summary className="cursor-pointer text-warning">
            第 {a.attempt} 次的 SQL 無法執行 → 已把錯誤訊息回饋給模型重寫
          </summary>
          <p className="mt-1 break-all font-mono text-xs text-ink-80">{a.error}</p>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap font-mono text-xs text-ink-48">{a.sql}</pre>
        </details>
      ))}

      {(sql || generating) && (
        <div>
          <div className="mb-1 flex items-center justify-between text-xs text-ink-48">
            <span className="font-semibold">產生的 SQL</span>
            {last?.ok && !generating && <span className="text-success">✓ 唯讀執行成功 · {result?.exec_ms ?? 0} ms</span>}
            {rejected && <span className="font-semibold text-danger">✕ 執行前已攔下</span>}
          </div>
          <pre
            ref={codeRef}
            className={`max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-tile p-4 font-mono text-[12.5px] leading-relaxed text-[var(--code-text)] [--color-accent:var(--color-accent-on-dark)] ${
              generating ? "caret" : ""
            } ${rejected ? "line-through decoration-danger decoration-2 ring-2 ring-danger" : ""}`}
          >
            {sql || "等待模型回應…"}
          </pre>
        </div>
      )}

      {result && <ResultTable result={result} />}

      {answer && (
        <p className={`whitespace-pre-line leading-relaxed ${status === "answering" ? "caret" : ""}`}>{answer}</p>
      )}

      {rejected && (
        <div className="rounded-lg border border-danger/30 bg-danger-soft p-3 text-sm">
          <p className="font-semibold text-danger">已拒絕：庫存查詢只能讀取資料</p>
          <p className="text-ink-80">
            模型照著問題寫出了修改資料的指令，系統在執行前就攔下（靜態檢查＋唯讀連線＋白名單三道保護），
            <b className="font-semibold text-ink">沒有任何資料被修改</b>，也不會讓模型改寫成查詢後假裝「已完成」。
          </p>
          <p className="mt-1 font-mono text-[11px] text-ink-48">
            {error.code} · request_id: {error.request_id}
          </p>
        </div>
      )}

      {error && !rejected && (
        <ErrorMessage
          title={error.code === "SQL_FAILED" ? "沒辦法把這個問題轉成 SQL" : "查詢失敗"}
          message={error.message}
          code={error.code}
          requestId={error.request_id}
        />
      )}

      {done && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-48">
          <EgressBadge egress={done.egress} />
          {done.fallback && (
            <span title={done.fallback_reason ?? ""} className="rounded-full bg-warning-soft px-2 py-0.5 font-semibold text-warning">
              本地備援模型
            </span>
          )}
          <span className="font-mono">{done.model}</span>
          <span>
            寫 SQL {seconds(done.latency_ms.sql)} · 執行 {done.latency_ms.exec} ms · 共 {seconds(done.latency_ms.total)}
          </span>
          {done.attempts > 1 && <span className="text-warning">修正 {done.attempts - 1} 次</span>}
        </div>
      )}
    </div>
  );
}

function ResultTable({ result }: { result: SqlResultEvent }) {
  if (result.row_count === 0)
    return (
      <p className="rounded-lg border border-dashed border-hairline px-3 py-2 text-sm text-ink-80">
        查詢結果 0 筆（模型不會編造答案）
      </p>
    );
  const numeric = result.columns.map((_, i) => result.rows.every((r) => r[i] == null || typeof r[i] === "number"));
  const fmt = (v: string | number | null) =>
    v == null ? "—" : typeof v === "number" ? v.toLocaleString("zh-TW", { maximumFractionDigits: 2 }) : v;
  return (
    <div>
      <div className="max-h-80 overflow-auto rounded-lg border border-hairline bg-card">
        <table className="w-full text-sm">
          <thead className="t-caption-strong sticky top-0 bg-parchment text-left text-ink-48">
            <tr>
              {result.columns.map((c, i) => (
                <th key={i} className={`whitespace-nowrap px-3 py-2 font-semibold ${numeric[i] ? "text-right" : ""}`}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((r, ri) => (
              <tr key={ri} className="border-t border-hairline">
                {r.map((v, i) => (
                  <td
                    key={i}
                    className={`px-3 py-2 align-top ${numeric[i] ? "text-right tabular-nums" : ""} ${
                      typeof v === "number" && v < 0 ? "text-danger" : ""
                    }`}
                  >
                    {fmt(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-xs text-ink-48">
        {result.row_count} 筆{result.truncated && `（只顯示前 ${result.row_count} 筆）`}
      </p>
    </div>
  );
}
