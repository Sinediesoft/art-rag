import { useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useCadEvalRuns, useEvalRuns, useHealth } from "../api/hooks";
import { Loading } from "../components/common/Feedback";
import { formatTaipei, seconds, STRATEGY_LABEL } from "../lib/format";

function Card({ title, children, className = "" }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-2xl border border-line bg-card p-4 shadow-sm ${className}`}>
      <h2 className="mb-3 font-serif text-lg font-bold">{title}</h2>
      {children}
    </section>
  );
}

const Dot = ({ ok }: { ok: boolean }) => (
  <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${ok ? "bg-jade" : "bg-seal"}`} />
);

const pct = (v: unknown) => (typeof v === "number" ? `${(v * 100).toFixed(0)}%` : "—");

export function AdminPage() {
  const { data: h, isLoading } = useHealth();
  const evals = useEvalRuns();
  const cadEvals = useCadEvalRuns();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);

  if (isLoading || !h) return <Loading />;
  const m = h.manifest as Record<string, any>;

  const toggleOutage = async () => {
    setBusy(true);
    await api.setOutage(!h.outage_simulated).finally(() => setBusy(false));
    await qc.invalidateQueries({ queryKey: ["health"] });
  };

  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-center gap-3">
        <h1 className="font-serif text-2xl font-black">系統狀態與評估</h1>
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-bold ${
            h.status === "ok" ? "bg-jade-soft text-jade" : "bg-amber-soft text-amber"
          }`}
        >
          {h.status === "ok" ? "全部正常" : "部分異常"}
        </span>
      </header>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="知識庫與索引">
          <dl className="grid grid-cols-[7em_1fr] gap-y-1.5 text-sm">
            <dt className="text-ink-faint">知識庫版本</dt>
            <dd className="font-mono">{h.kb_version}</dd>
            <dt className="text-ink-faint">畫作／段落</dt>
            <dd>
              {m.artwork_count} 幅／{m.chunk_count} 段
            </dd>
            <dt className="text-ink-faint">工廠圖紙／段落</dt>
            <dd>
              {m.part_count ?? 0} 張／{m.part_chunk_count ?? 0} 段
            </dd>
            <dt className="text-ink-faint">影像模型</dt>
            <dd className="break-all font-mono text-xs">
              {m.models?.image?.name}@{String(m.models?.image?.revision).slice(0, 7)} · {m.models?.image?.dim} 維
            </dd>
            <dt className="text-ink-faint">文字模型</dt>
            <dd className="break-all font-mono text-xs">
              {m.models?.text?.name}@{String(m.models?.text?.revision).slice(0, 7)} · {m.models?.text?.dim} 維
            </dd>
            <dt className="text-ink-faint">索引建立</dt>
            <dd>{m.created_at ? formatTaipei(m.created_at) : "—"}</dd>
            <dt className="text-ink-faint">一致性</dt>
            <dd className="flex items-start gap-2">
              <Dot ok={h.index_consistent} />
              <span>{h.index_consistent ? "manifest 與設定一致" : h.index_problems.join("；")}</span>
            </dd>
          </dl>
        </Card>

        <Card title="生成端">
          <ul className="flex flex-col gap-2 text-sm">
            {Object.entries(h.strategies).map(([k, s]) => (
              <li key={k} className="flex items-start gap-2">
                <Dot ok={s.available} />
                <div className="min-w-0">
                  <p className="font-medium">
                    {s.label} <span className="font-mono text-xs text-ink-faint">{s.model}</span>
                  </p>
                  <p className="break-all text-xs text-ink-faint">{s.detail}</p>
                </div>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-ink-faint">
            LLM_MODE={h.llm_mode} · EMBED_MODE={h.embed_mode} · ALLOW_CLOUD={String(h.allow_cloud)} · 資料庫{" "}
            {h.db ? "正常" : "異常"}
          </p>
        </Card>
      </div>

      {h.demo_controls && (
        <Card title="容錯展示" className={h.outage_simulated ? "border-amber/40 bg-amber-soft/40" : ""}>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <p className="flex-1 text-sm text-ink-soft">
              模擬主推論伺服器斷線。之後的混合式問答會在 5 秒內改走本地備援模型，回答上標註「本地備援模型」；
              本地都失敗就暫停服務，不改走雲端。直接關掉 Ollama 則主力與同主機的備援都會停，可用來展示「服務暫停」。
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={toggleOutage}
              className={`shrink-0 rounded-xl px-4 py-2.5 font-bold transition disabled:opacity-50 ${
                h.outage_simulated ? "bg-jade text-white" : "bg-amber text-white"
              }`}
            >
              {h.outage_simulated ? "恢復推論伺服器" : "模擬斷線"}
            </button>
          </div>
        </Card>
      )}

      <Card title="評估結果">
        {evals.data?.runs.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead className="text-left text-xs text-ink-faint">
                <tr>
                  <th className="py-1">時間</th>
                  <th>kb／prompt</th>
                  <th>以圖搜圖 Top-1</th>
                  <th>未收錄拒答</th>
                  <th>各策略問答結果</th>
                </tr>
              </thead>
              <tbody>
                {evals.data.runs.map((r) => (
                  <tr key={r.run_id} className="border-t border-line align-top">
                    <td className="py-1.5">{formatTaipei(r.created_at)}</td>
                    <td className="font-mono text-xs">
                      {r.kb_version}／{r.prompt_version}
                    </td>
                    <td>{pct(r.summary.image_top1)}</td>
                    <td>{pct(r.summary.reject_rate)}</td>
                    <td className="text-xs">
                      {Object.entries(r.by_strategy).map(([k, v]: [string, any]) => (
                        <div key={k}>
                          {STRATEGY_LABEL[k] ?? k}：引用 {pct(v.citation_ok)} · 關鍵字 {pct(v.answer_ok)} · P95{" "}
                          {seconds(v.p95_total_ms)}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-soft">
            尚無評估結果。執行 <code className="font-mono">make eval</code> 產生。
          </p>
        )}
      </Card>

      <Card title="工廠圖紙評估（Ortho2CAD）">
        {cadEvals.data?.runs.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[620px] text-sm">
              <thead className="text-left text-xs text-ink-faint">
                <tr>
                  <th className="py-1">時間</th>
                  <th>圖紙辨識 Top-1</th>
                  <th>未收錄拒答</th>
                  <th>3D 重建（可執行／IoU／外框對齊 IoU）</th>
                </tr>
              </thead>
              <tbody>
                {cadEvals.data.runs.map((r: any) => (
                  <tr key={r.run_id} className="border-t border-line align-top">
                    <td className="py-1.5">{formatTaipei(r.created_at)}</td>
                    <td>
                      {pct(r.identification?.top1)}
                      {r.identification && (
                        <span className="text-xs text-ink-faint">（{r.identification.n_known} 張）</span>
                      )}
                    </td>
                    <td>
                      {pct(r.identification?.reject_rate)}
                      {r.identification && (
                        <span className="text-xs text-ink-faint">（{r.identification.n_unknown} 張）</span>
                      )}
                    </td>
                    <td className="text-xs">
                      {Object.entries(r.reconstruction ?? {}).map(([k, v]: [string, any]) => (
                        <div key={k}>
                          {k === "ortho2cad" ? "Ortho2CAD" : "Qwen3-VL 未微調"}：{pct(v.valid_rate)}／
                          {v.mean_iou.toFixed(3)}／{(v.mean_iou_bbox ?? 0).toFixed(3)} · 中位數{" "}
                          {seconds(v.median_total_ms)}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-soft">
            尚無評估結果。執行 <code className="font-mono">make eval-cad</code> 產生。
          </p>
        )}
        {h.recent_cad.length > 0 && (
          <>
            <h3 className="mb-1 mt-4 text-sm font-bold text-ink-soft">最近的 3D 重建</h3>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-sm">
                <thead className="text-left text-xs text-ink-faint">
                  <tr>
                    <th className="py-1">時間</th>
                    <th>圖紙</th>
                    <th>模型</th>
                    <th>結果</th>
                    <th>總計</th>
                    <th>外送</th>
                  </tr>
                </thead>
                <tbody>
                  {h.recent_cad.map((c: any) => (
                    <tr key={c.request_id} className="border-t border-line">
                      <td className="whitespace-nowrap py-1.5 pr-2 text-xs">{formatTaipei(c.created_at)}</td>
                      <td className="pr-2 text-xs">
                        {c.part_id ? (
                          <Link to={`/drawings/${c.part_id}/reconstruct?job=${c.job_id}`} className="text-steel underline">
                            {c.part_id}
                          </Link>
                        ) : (
                          "未收錄圖紙"
                        )}
                        {c.image_id && <span className="text-ink-faint">（照片）</span>}
                      </td>
                      <td className="whitespace-nowrap pr-2 text-xs">
                        {c.strategy === "ortho2cad" ? "Ortho2CAD" : "Qwen3-VL 對照組"}
                      </td>
                      <td className="text-xs">
                        {c.ok ? (
                          <span className="text-jade">
                            ✓{c.iou != null && ` IoU ${c.iou.toFixed(2)}`}
                          </span>
                        ) : (
                          <span className="text-seal" title={c.error ?? ""}>
                            ✗ 無法執行
                          </span>
                        )}
                      </td>
                      <td className="text-xs">{seconds(c.total_ms)}</td>
                      <td className="text-xs text-jade">0</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      <Card title="最近問答紀錄">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="text-left text-xs text-ink-faint">
              <tr>
                <th className="py-1">時間</th>
                <th>問題</th>
                <th>生成端</th>
                <th>首字</th>
                <th>總計</th>
                <th>外送</th>
              </tr>
            </thead>
            <tbody>
              {h.recent_chats.map((c: any) => (
                <tr key={c.request_id} className="border-t border-line">
                  <td className="whitespace-nowrap py-1.5 pr-2 text-xs">{formatTaipei(c.created_at)}</td>
                  <td className="max-w-[16rem] truncate pr-2">{c.question}</td>
                  <td className="whitespace-nowrap pr-2 text-xs">
                    {STRATEGY_LABEL[c.strategy_used] ?? c.strategy_used}
                    {c.fallback ? <span className="ml-1 text-amber">（備援）</span> : null}
                  </td>
                  <td className="text-xs">{seconds(c.first_token_ms)}</td>
                  <td className="text-xs">{seconds(c.total_ms)}</td>
                  <td className={`text-xs ${c.egress_bytes ? "text-seal" : "text-jade"}`}>
                    {c.egress_bytes ? `${(c.egress_bytes / 1024).toFixed(1)} KB` : "0"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="新增畫作（不改程式）">
        <ol className="list-decimal space-y-1 pl-5 text-sm text-ink-soft">
          <li>
            在 <code className="font-mono">kb/artworks/</code> 加一個 <code className="font-mono">&lt;id&gt;.json</code>，
            圖片放 <code className="font-mono">kb/images/</code>
          </li>
          <li>
            遞增 <code className="font-mono">kb/VERSION</code>
          </li>
          <li>
            執行 <code className="font-mono">make index</code>：後端會自動載入新索引，不必重啟
          </li>
        </ol>
        <p className="mt-2 text-xs text-ink-faint">
          展示時可用 <code className="font-mono">make demo-add</code> 一次加入〈早春圖〉與〈睡蓮〉，
          <code className="font-mono">make demo-reset</code> 還原。
        </p>
        <h3 className="mb-1 mt-4 text-sm font-bold text-ink-soft">新增工廠圖紙</h3>
        <ol className="list-decimal space-y-1 pl-5 text-sm text-ink-soft">
          <li>
            在 <code className="font-mono">kb/parts/</code> 加 <code className="font-mono">&lt;id&gt;.json</code>，
            標準模型寫在 <code className="font-mono">kb/cad/&lt;id&gt;.py</code>（CadQuery，最後指定給 <code className="font-mono">solid</code>）
          </li>
          <li>
            執行 <code className="font-mono">make drawings</code> 產生圖紙、遞增版本、<code className="font-mono">make index</code>
          </li>
        </ol>
        <p className="mt-2 text-xs text-ink-faint">
          展示時一併由 <code className="font-mono">make demo-add</code> 加入〈治具定位板〉。
        </p>
      </Card>
    </div>
  );
}
