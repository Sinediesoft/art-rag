import { Fragment } from "react";

/** 來源引用標籤 [n]；[0] 代表模型自述「無來源」 */
export function CitationTag({
  refNo,
  active,
  onClick,
}: {
  refNo: number;
  active?: boolean;
  onClick?: (n: number) => void;
}) {
  if (refNo === 0) {
    return (
      <span className="mx-0.5 inline-flex items-center rounded bg-warning-soft px-1.5 align-[1px] text-[11px] font-normal text-warning">
        無來源
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onClick?.(refNo)}
      className={`mx-0.5 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1 align-[1px] text-[11px] font-semibold transition ${
        active ? "bg-accent text-white" : "bg-accent-soft text-accent hover:bg-accent hover:text-white"
      }`}
      aria-label={`來源 ${refNo}`}
    >
      {refNo}
    </button>
  );
}

/** 把回答中的 [1]、[1][3] 轉成可點的引用標籤 */
export function AnswerText({
  text,
  activeRef,
  onCite,
}: {
  text: string;
  activeRef?: number | null;
  onCite?: (n: number) => void;
}) {
  const parts = text.split(/(\[\d+\])/g);
  return (
    <>
      {parts.map((p, i) => {
        const m = p.match(/^\[(\d+)\]$/);
        return m ? (
          <CitationTag key={i} refNo={Number(m[1])} active={activeRef === Number(m[1])} onClick={onCite} />
        ) : (
          <Fragment key={i}>{p}</Fragment>
        );
      })}
    </>
  );
}
