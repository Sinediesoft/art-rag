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

// ---- AI 助理 ----
const msgsEl = $('msgs');
const CHIPS = {
  working: ['這一步在做什麼？', '還要多久？', '可以關掉視窗嗎？'],
  error: ['這個錯誤要怎麼修？', '重試之前要先做什麼？'],
  warning: ['打叉的功能還能用嗎？', '要怎麼修？'],
  reboot: ['為什麼要重新開機？', '重新開機後要做什麼？'],
  info: ['這個結果是什麼意思？'],
};
let asking = false;
let postedDiagnosis = '';

function addMsg(kind, text, who) {
  const el = document.createElement('div');
  el.className = `msg ${kind}`;
  if (who) {
    const tag = document.createElement('span');
    tag.className = 'who';
    tag.textContent = who;
    el.appendChild(tag);
  }
  el.appendChild(document.createTextNode(text));
  msgsEl.appendChild(el);
  msgsEl.scrollTop = msgsEl.scrollHeight;
  return el;
}

async function ask(question) {
  const q = question.trim();
  if (!q || asking) return;
  asking = true;
  $('q').value = '';
  $('q').disabled = $('send').disabled = true;
  addMsg('me', q);
  const wait = addMsg('bot wait', '思考中…（第一次要載入模型，約 10～30 秒）', 'AI 助理');
  const r = await window.artrag.ask(q).catch((err) => ({ error: err.message }));
  wait.remove();
  if (r.answer) addMsg('bot', r.answer + (r.slow ? '\n（這台沒有 NVIDIA 顯示卡，回答會比較慢）' : ''), 'AI 助理');
  else addMsg('bot err', r.error || '沒有回答', 'AI 助理');
  asking = false;
  $('q').disabled = $('send').disabled = false;
  $('q').focus();
}

function showChips(kind) {
  const chips = $('chips');
  chips.replaceChildren();
  for (const text of CHIPS[kind] || []) {
    const el = document.createElement('button');
    el.type = 'button';
    el.textContent = text;
    el.addEventListener('click', () => ask(text));
    chips.appendChild(el);
  }
}

// 失敗時把紀錄最後的「故障診斷」貼到助理這邊：不用自己去紀錄裡找
function postDiagnosis() {
  const text = logEl.textContent;
  const at = text.lastIndexOf('── 故障診斷 ──');
  if (at < 0) return;
  const body = text.slice(at + '── 故障診斷 ──'.length).trim();
  if (!body || body === postedDiagnosis) return;
  postedDiagnosis = body;
  addMsg('bot', `我看了這次的紀錄：\n${body}\n\n還有不清楚的地方可以直接問我。`, 'AI 助理・故障診斷');
}

$('ask').addEventListener('submit', (event) => {
  event.preventDefault();
  ask($('q').value);
});
addMsg('bot', '安裝過程有任何問題都可以問我，例如「這一步在做什麼？」「為什麼要重新開機？」。出問題時我會先看紀錄，告訴你原因和修法。', 'AI 助理');

window.artrag.onState((state) => {
  document.body.className = state.kind;
  showChips(state.kind);
  if (['error', 'warning', 'info'].includes(state.kind)) postDiagnosis();
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
