import type { EntryDomain } from "../../shell/types";

const CHOICES: [EntryDomain | null, string][] = [
  [null, "自動判斷"],
  ["art", "畫作"],
  ["mfg", "工廠"],
];

/**
 * 入口輸入框下方的分類（docs/adr/040）：選了畫作或工廠，智慧助理只在那個領域裡分流。
 * 只縮小分流範圍，權限照樣由後端依身分檢查；預設自動判斷。
 */
export function DomainPicker({ value, onChange }: { value: EntryDomain | null; onChange: (d: EntryDomain | null) => void }) {
  return (
    <div className="entry-domain">
      <span className="entry-domain__label">問題類別</span>
      <div className="segmented segmented--small segmented--3" role="radiogroup" aria-label="問題類別">
        {CHOICES.map(([d, label]) => (
          <button key={label} type="button" role="radio" aria-checked={value === d} className={value === d ? "is-on" : ""} onClick={() => onChange(d)}>
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
