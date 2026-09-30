import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, assetUrl, type Strategy } from "../../api/client";
import { usePart } from "../../api/hooks";
import { ChatAnswer } from "../../components/ChatAnswer";
import { Loading } from "../../components/common/Feedback";
import { ConfidentialityBadge } from "../../components/parts/PartBadges";

const SUGGESTIONS = [
  "這個零件的加工製程是什麼？",
  "有哪些公差要求？要怎麼檢驗？",
  "材料和熱處理是什麼？",
  "重量大約多少？",
  "過去發生過什麼不良？怎麼改善？",
  "這個零件一件的單價是多少？",
];

// 機密圖紙只走本地生成端；雲端對照組在後端就會拒絕（CLOUD_CONFIDENTIAL_FORBIDDEN）
const STRATEGIES: { value: Strategy; label: string; hint: string }[] = [
  { value: "hybrid", label: "混合式", hint: "本地 Qwen3-VL＋圖紙知識庫" },
  { value: "mock", label: "mock", hint: "不呼叫模型" },
];

interface Turn {
  id: number;
  question: string;
  strategy: Strategy;
}

export function PartChatPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const imageId = params.get("image");
  const { data: p, isLoading } = usePart(id);
  const [strategy, setStrategy] = useState<Strategy>("hybrid");
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length]);

  const ask = (question: string) => {
    if (!question.trim()) return;
    setTurns((t) => [...t, { id: Date.now(), question: question.trim(), strategy }]);
    setInput("");
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    ask(input);
  };

  if (isLoading || !p) return <Loading />;

  return (
    <div className="flex flex-col gap-4">
      <Link to={`/drawings/${p.id}`} className="flex items-center gap-3 rounded-xl border border-line bg-card p-2 pr-4">
        <img
          src={imageId ? api.uploadedImageUrl(imageId) : assetUrl(p.thumb_url)}
          alt=""
          className="h-14 w-14 rounded-lg bg-white object-contain"
        />
        <div className="min-w-0 flex-1">
          <p className="truncate text-lg font-bold">{p.name.zh}</p>
          <p className="truncate text-xs text-ink-faint">
            {p.part_no} · {p.drawing_no} rev.{p.revision} · {p.material}
            {imageId && " · 模型會看你拍的圖紙"}
          </p>
        </div>
        <ConfidentialityBadge level={p.confidentiality} />
      </Link>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-bold text-ink-faint">生成端</span>
        <div className="flex rounded-xl border border-line bg-card p-1">
          {STRATEGIES.map((s) => (
            <button
              key={s.value}
              type="button"
              title={s.hint}
              onClick={() => setStrategy(s.value)}
              className={`rounded-lg px-3 py-1 text-sm font-medium transition ${
                strategy === s.value ? "bg-ink text-paper" : "text-ink-soft hover:bg-paper-deep"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <span className="text-xs text-ink-faint">機密圖紙不提供雲端生成端，連對照組也不送</span>
      </div>

      {turns.length === 0 && (
        <div className="rounded-xl border border-dashed border-line p-4">
          <p className="mb-2 text-sm text-ink-soft">可以這樣問：</p>
          <div className="flex flex-wrap gap-2">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => ask(s)}
                className="rounded-full border border-line bg-card px-3 py-1.5 text-sm transition hover:border-steel hover:text-steel"
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-5">
        {turns.map((t) => (
          <div key={t.id} className="flex flex-col gap-2">
            <div className="self-end rounded-2xl rounded-br-sm bg-ink px-4 py-2 text-paper">{t.question}</div>
            <div className="rounded-2xl rounded-bl-sm border border-line bg-card p-4 shadow-sm">
              <ChatAnswer
                request={{ question: t.question, part_id: p.id, image_id: imageId, strategy: t.strategy }}
              />
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <form
        onSubmit={submit}
        className="sticky bottom-20 z-10 flex gap-2 rounded-2xl border border-line bg-card/95 p-2 shadow-lg backdrop-blur sm:bottom-4"
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="問這張圖紙的製程、公差、材料…"
          maxLength={500}
          className="min-w-0 flex-1 bg-transparent px-3 py-2 outline-none"
        />
        <button
          className="shrink-0 rounded-xl bg-steel px-4 py-2 font-bold text-white transition hover:bg-steel-deep disabled:opacity-50"
          disabled={!input.trim()}
        >
          送出
        </button>
      </form>
    </div>
  );
}
