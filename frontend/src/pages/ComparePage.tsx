import { useState, type FormEvent } from "react";
import type { ChatRequest } from "../api/client";
import { useArtworks } from "../api/hooks";
import { ChatAnswer } from "../components/ChatAnswer";
import { Loading } from "../components/common/Feedback";

const COLUMNS: { key: string; label: string; note: string; body: Partial<ChatRequest> }[] = [
  { key: "hybrid", label: "C. 混合式", note: "本地 VLM＋檢索（主架構）", body: { strategy: "hybrid" } },
  {
    key: "hybrid-norag",
    label: "混合式・關檢索",
    note: "同一模型不給參考資料（檢索增益對照）",
    body: { strategy: "hybrid", use_retrieval: false },
  },
  {
    key: "api_nokb",
    label: "A1. 雲端・無檢索",
    note: "只送照片與問題（等同直接用雲端大模型）→ 證明需要 RAG",
    body: { strategy: "api_nokb" },
  },
  {
    key: "api_kb",
    label: "A2. 雲端＋檢索",
    note: "連同知識庫段落一起送出 → 品質上限，但資料外流",
    body: { strategy: "api_kb" },
  },
  { key: "lora", label: "B. 本地 LoRA", note: "選做：插槽已保留", body: { strategy: "lora" } },
];

const QUESTIONS = ["畫家的簽名藏在哪裡？是誰發現的？", "這幅畫用了什麼技法？", "畫面上有哪些人物？", "這幅畫是在哪裡畫的？"];

export function ComparePage() {
  const { data } = useArtworks();
  const [artworkId, setArtworkId] = useState<string>("");
  const [question, setQuestion] = useState(QUESTIONS[0]);
  const [run, setRun] = useState<{ artworkId: string; question: string; n: number } | null>(null);
  const selected = artworkId || data?.items[0]?.id || "";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (question.trim() && selected) setRun({ artworkId: selected, question: question.trim(), n: Date.now() });
  };

  if (!data) return <Loading />;

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="t-display">策略比較</h1>
        <p className="mt-2 text-sm text-ink-80">
          同一個問題並排送給不同生成端。所有欄位共用同一個檢索層與 prompt 模板，只換最後的生成端，
          所以差異只反映生成端本身；每欄標示資料是否送出本機。雲端欄位只是對照組（需後端
          ALLOW_CLOUD=true），這頁只能選知識庫畫作、不開放上傳照片；比較模式不啟用備援。
        </p>
      </header>

      <form onSubmit={submit} className="card flex flex-col gap-3 p-5">
        <div className="flex flex-col gap-2 sm:flex-row">
          <select
            value={selected}
            onChange={(e) => setArtworkId(e.target.value)}
            className="field sm:w-56"
          >
            {data.items.map((a) => (
              <option key={a.id} value={a.id}>
                〈{a.title_zh}〉{a.artist_zh}
              </option>
            ))}
          </select>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            className="field min-w-0 flex-1"
          />
          <button className="btn-primary">
            並排比較
          </button>
        </div>
        <div className="flex flex-wrap gap-2">
          {QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => setQuestion(q)}
              className="chip text-ink-80 hover:border-accent hover:text-accent"
            >
              {q}
            </button>
          ))}
        </div>
      </form>

      {run && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {COLUMNS.map((c) => (
            <section key={`${c.key}-${run.n}`} className="card flex flex-col gap-3 p-5">
              <header className="border-b border-hairline pb-2">
                <h2 className="text-lg font-semibold">{c.label}</h2>
                <p className="text-xs text-ink-48">{c.note}</p>
              </header>
              <ChatAnswer
                compact
                showSources={c.body.use_retrieval !== false}
                request={{
                  question: run.question,
                  artwork_id: run.artworkId,
                  allow_fallback: false,
                  ...c.body,
                }}
              />
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
