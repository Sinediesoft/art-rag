import { useStatus } from "../../api/hooks";
import { SWITCH_TEXT, SWITCHES, setSetting, useSettings, type Choice } from "../../shell/settings";
import { useShell } from "../../shell/store";
import { Icon } from "./Icons";

/**
 * 設定面板（頁首右上角，Q-2026-10-12-02）：問答的三個開關（深淺色改用頁首的太陽／月亮圖示）。
 * 只存在這個瀏覽器（shell/settings.ts），從下一題開始生效；第 1～7 段安全關卡由伺服器決定，這裡沒有開關。
 */
export function SettingsMenu() {
  const settings = useSettings();
  const { data: status } = useStatus();
  const server = status?.chat_defaults;
  return (
    <div className="settings">
      <section className="settings__group">
        <p className="settings__title">問答</p>
        {SWITCHES.map((k) => {
          const t = SWITCH_TEXT[k];
          // 伺服器預設讀不到（還在載入、/status 失敗）時只寫「預設」，不猜
          const options: [Choice, string][] = [
            ["server", server ? `預設（${server[k] ? "開" : "關"}）` : "預設"],
            ["on", "開"],
            ["off", "關"],
          ];
          return (
            <div key={k} className="settings__item">
              <div className="settings__row">
                <span className="settings__text">
                  {t.label}
                  <small>{t.cost}</small>
                </span>
                <div className="segmented segmented--small segmented--3" role="radiogroup" aria-label={t.label}>
                  {options.map(([v, label]) => (
                    <button key={v} type="button" role="radio" aria-checked={settings[k] === v} className={settings[k] === v ? "is-on" : ""} onClick={() => setSetting(k, v)}>
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <p className="settings__desc">{t.desc}</p>
            </div>
          );
        })}
      </section>
      <p className="settings__foot">只存在這個瀏覽器，換身分不會變；改了從下一題開始生效。「預設」照這台伺服器的設定。安全關卡（第 1～7 段）由伺服器決定，這裡不能改。</p>
    </div>
  );
}

/**
 * 頁首的明暗切換（來源：ArtRAG-整合版demo 的太陽／月亮圖示）：入口、模組、功能頁的頁首各有一顆，切的是同一個設定。
 * className 跟著所在頁首的按鈕樣式（入口 topbar__btn、模組 mtop__btn）
 */
export function ThemeToggle({ className = "mtop__btn mtop__theme" }: { className?: string }) {
  const shell = useShell();
  const label = shell.theme === "dark" ? "切換成明亮模式" : "切換成暗色模式";
  return (
    <button type="button" className={className} onClick={() => shell.setTheme(shell.theme === "dark" ? "light" : "dark")} title={label} aria-label={label}>
      <Icon name={shell.theme === "dark" ? "sun" : "moon"} strokeWidth={1.5} />
    </button>
  );
}
