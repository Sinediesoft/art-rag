// 啟動畫面：顯示主行程傳來的狀態與腳本輸出
const $ = (id) => document.getElementById(id);
const logEl = $('log');

// 腳本的步驟標題：install-offline.ps1 是「▶ …」，start-services.ps1 是「… 啟動 …」「✓ …」
function stepOf(line) {
  const m = line.match(/^\s*(?:▶|…|✓)\s*(.+)$/);
  return m ? m[1] : null;
}

window.artrag.onLog((line) => {
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 8;
  logEl.textContent += `${line}\n`;
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  if (document.body.classList.contains('working')) {
    const step = stepOf(line);
    if (step) $('step').textContent = step;
  }
});

window.artrag.onState((state) => {
  document.body.className = state.kind;
  $('title').textContent = state.title || '';
  $('detail').textContent = state.detail || '';
  if (state.kind !== 'working') $('step').textContent = '';
  // 出問題時直接攤開紀錄：診斷結果在最後面
  if (['error', 'warning', 'info'].includes(state.kind)) {
    $('more').open = true;
    logEl.scrollTop = logEl.scrollHeight;
  }
  const actions = $('actions');
  actions.replaceChildren();
  for (const b of state.buttons || []) {
    const el = document.createElement('button');
    el.textContent = b.label;
    if (b.primary) el.className = 'primary';
    el.addEventListener('click', () => window.artrag.action(b.id));
    actions.appendChild(el);
  }
  const first = actions.querySelector('button');
  if (first) first.focus();
});
