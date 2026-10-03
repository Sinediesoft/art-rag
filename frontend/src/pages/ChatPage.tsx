import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, assetUrl, type Strategy } from "../api/client";
import { useArtwork } from "../api/hooks";
import { ChatAnswer } from "../components/ChatAnswer";
import { Loading } from "../components/common/Feedback";

const SUGGESTIONS = [
  "這幅畫畫了什麼？",
  "作者是誰？什麼時候畫的？",
  "用了什麼特別的技法？",
  "有什麼創作背景或小故事？",
  "這幅畫當年賣了多少錢？",
];

// 問答頁只提供本地生成端：使用者的照片與問題不離開本機。雲端對照組只在策略比較頁。
const STRATEGIES: { value: Strategy; label: string; hint: string }[] = [
  { value: "hybrid", label: "混合式", hint: "本地 VLM（主架構）" },
  { value: "lora", label: "LoRA", hint: "選做（本地）" },
  { value: "mock", label: "mock", hint: "不呼叫模型" },
];

interface Turn {
  id: number;
  question: string;
  strategy: Strategy;
}

export function ChatPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const imageId = params.get("image");
  const { data: a, isLoading } = useArtwork(id);
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

  if (isLoading || !a) return <Loading />;

  return (
    <div className="flex flex-col gap-4">
      <Link
        to={`/artworks/${a.id}`}
        className="card flex items-center gap-3 p-2 pr-4 transition hover:border-ink-48"
      >
        <img
          src={imageId ? api.uploadedImageUrl(imageId) : assetUrl(a.thumb_url)}
          alt=""
          className="h-14 w-14 rounded-lg object-cover"
        />
        <div className="min-w-0">
          <p className="truncate font-display text-lg font-semibold text-ink">〈{a.title.zh}〉</p>
          <p className="truncate text-xs text-ink-48">
            {a.artist.zh} · {a.date_text}
            {imageId && " · 模型會看你拍的照片"}
          </p>
        </div>
      </Link>

      <div className="flex flex-wrap items-center gap-2">
        <span className="t-fine font-semibold text-ink-48">生成端</span>
        <div className="flex rounded-full border border-hairline bg-card p-1">
          {STRATEGIES.map((s) => (
            <button
              key={s.value}
              type="button"
              title={s.hint}
              onClick={() => setStrategy(s.value)}
              className={`rounded-full px-3 py-1 text-sm font-normal transition ${
                strategy === s.value ? "bg-accent text-white" : "text-ink-80 hover:bg-parchment-deep"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {turns.length === 0 && (
        <div className="card p-5">
          <p className="t-caption mb-3 text-ink-80">可以這樣問：</p>
          <div className="flex flex-wrap gap-2">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => ask(s)}
                className="chip hover:border-accent hover:text-accent"
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
            <div className="max-w-[85%] self-end rounded-2xl rounded-br-sm bg-accent px-4 py-2 text-white">{t.question}</div>
            <div className="card p-5">
              <ChatAnswer
                request={{
                  question: t.question,
                  artwork_id: a.id,
                  image_id: imageId,
                  strategy: t.strategy,
                }}
              />
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <form
        onSubmit={submit}
        className="frosted sticky bottom-20 z-10 flex items-center gap-2 rounded-2xl border border-hairline p-2 sm:bottom-4"
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="針對這幅畫提問…"
          maxLength={500}
          className="field min-w-0 flex-1"
        />
        <button className="btn-primary shrink-0" disabled={!input.trim()}>
          送出
        </button>
      </form>
    </div>
  );
}
