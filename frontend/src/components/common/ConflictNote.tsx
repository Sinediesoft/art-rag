import type { SourcesEvent } from "../../api/sse";
import { CitationTag } from "./CitationTag";

type ConflictCheck = NonNullable<SourcesEvent["conflict_check"]>;

/** 回答前的矛盾檢查判為有矛盾（docs/adr/028）：提醒觀眾參考資料的說法不一致，編號可點到該段 */
export function ConflictNote({
  check,
  onCite,
}: {
  check: ConflictCheck | null | undefined;
  onCite?: (n: number) => void;
}) {
  if (!check || check.fallback || !check.conflict || check.refs.length < 2) return null;
  return (
    <p className="rounded-md bg-warning-soft px-3 py-2 text-xs leading-relaxed text-warning">
      參考資料
      {check.refs.map((n) => (
        <CitationTag key={n} refNo={n} onClick={onCite} />
      ))}
      對這個問題的說法不一致，回答已分別列出；請以館方或正式文件為準。
    </p>
  );
}

/** 參考來源標題後面的小字：檢查過、沒有矛盾時才顯示 */
export function conflictCheckedLabel(check: ConflictCheck | null | undefined): string {
  return check && !check.fallback && !check.conflict ? " · 已比對，說法一致" : "";
}
