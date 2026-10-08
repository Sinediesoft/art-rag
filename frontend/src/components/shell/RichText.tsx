import type { ReactNode } from "react";

/**
 * 來源：ArtRAG-前端demo/source/src/components/RichText.tsx（段落、- 清單、**粗體**、[n] 引用）。
 * 加上本專案的引用規則：[0]＝模型自述無來源、[畫面]＝看附上的畫作圖片得到的（docs/adr/026），兩者都不能點。
 */
export function RichText({
  text,
  streaming = false,
  activeRef,
  onCite,
}: {
  text: string;
  streaming?: boolean;
  activeRef?: number | null;
  onCite?: (n: number) => void;
}) {
  const blocks = text.split(/\n{2,}/).filter((b) => b.trim());
  return (
    <div className={`prose${streaming ? " is-streaming" : ""}`}>
      {blocks.map((b, i) => {
        const lines = b.split("\n").filter((l) => l.trim());
        if (lines.length && lines.every((l) => /^\s*[-*・]\s/.test(l)))
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*[-*・]\s/, ""), activeRef, onCite)}</li>
              ))}
            </ul>
          );
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <span key={j}>
                {j > 0 && <br />}
                {inline(l, activeRef, onCite)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function inline(s: string, activeRef?: number | null, onCite?: (n: number) => void): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\*\*(.+?)\*\*|\[(\d+)\]|\[畫面\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1] !== undefined) out.push(<strong key={k++}>{m[1]}</strong>);
    else if (m[2] === undefined)
      out.push(
        <span key={k++} className="cite cite--image" title="這句是模型看附上的畫作圖片得到的，知識庫段落裡沒有寫，請自己看圖確認">
          畫面
        </span>,
      );
    else if (m[2] === "0")
      out.push(
        <span key={k++} className="cite cite--none" title="模型自述沒有來源">
          無來源
        </span>,
      );
    else {
      const n = Number(m[2]);
      out.push(
        <button key={k++} type="button" onClick={() => onCite?.(n)} className={`cite${activeRef === n ? " is-active" : ""}`} title={`來源 ${n}`} aria-label={`來源 ${n}`}>
          {n}
        </button>,
      );
    }
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
