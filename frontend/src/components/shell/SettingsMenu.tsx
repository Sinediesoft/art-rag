import { useStatus } from "../../api/hooks";
import { SWITCH_TEXT, SWITCHES, setSetting, useSettings, type Choice } from "../../shell/settings";
import { useShell } from "../../shell/store";
import type { EntryTheme } from "../../shell/theme";
import { Icon } from "./Icons";

const THEMES: [EntryTheme, string][] = [
  ["dark", "深色"],
  ["light", "淺色"],
];

/**
 * 設定面板（頁首右上角，Q-2026-10-12-02）：深淺色（入口與模組共用）、問答的三個開關。
 * 只存在這個瀏覽器（shell/settings.ts），從下一題開始生效；第 1～7 段安全關卡由伺服器決定，這裡沒有開關。
 */
export function SettingsMenu() {
  const shell = useShell();
  const settings = useSettings();
  const { data: status } = useStatus();
  const server = status?.chat_defaults;
  return (
    <div className="settings">
      <section className="settings__group">
        <p className="settings__title">外觀</p>
        <div className="settings__row">
          <span className="settings__text">
            深淺色
            <small>入口、工廠與藝術模組共用</small>
          </span>
          <div className="segmented segmented--small" role="radiogroup" aria-label="深淺色">
            {THEMES.map(([t, label]) => (
              <button key={t} type="button" role="radio" aria-checked={shell.theme === t} className={shell.theme === t ? "is-on" : ""} onClick={() => shell.setTheme(t)}>
                {label}
              </button>
            ))}
          </div>
        </div>
      </section>
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

/** 模組與功能頁頂列的明暗切換（來源：ArtRAG-整合版demo 的 mtop__theme）：和設定面板的「深淺色」是同一個設定 */
export function ThemeToggle() {
  const shell = useShell();
  const label = shell.theme === "dark" ? "切換成明亮模式" : "切換成暗色模式";
  return (
    <button type="button" className="mtop__btn mtop__theme" onClick={() => shell.setTheme(shell.theme === "dark" ? "light" : "dark")} title={label} aria-label={label}>
      <Icon name={shell.theme === "dark" ? "sun" : "moon"} strokeWidth={1.5} />
    </button>
  );
}
