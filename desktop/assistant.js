// 安裝畫面的 AI 助理（docs/adr/047）：用這台電腦上的模型回答安裝相關的問題，不連網。
// 安裝途中模型服務還沒起（最後一步才啟動）：離線包就先起包裡的 ollama serve，之後啟動服務時直接沿用。
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const ANSWER_TIMEOUT_MS = 180000;
const OLLAMA_START_TIMEOUT_MS = 30000;
const LOG_LINES = 15;
const ERROR_LINES = 10;
const ERRORISH = /error|exception|traceback|failed|fatal|refused|denied|not found|錯誤|失敗|找不到|拒絕|沒起來|不能用|✗/i;
const DIAG_MARK = '── 故障診斷 ──';

const SYSTEM_PROMPT = [
  '你是桌面程式「萊問」的安裝助理，用這台電腦上的模型回答，不連網。',
  '只回答和安裝、啟動、使用萊問有關的問題；用正體中文（台灣用語），簡短：最多 5 句，或最多 3 個步驟。',
  '看不出來就直接說不知道，不要編造檔案、設定或指令；不要叫使用者連網下載東西或安裝 Docker。',
  '先看【故障診斷的結論】和【紀錄裡的錯誤】，針對這次的狀況回答，和這次無關的步驟不要列；',
  '故障診斷已經有結論時以它為準，用白話解釋原因、告訴使用者下一步要按什麼。問題和目前狀況無關時，照一般情況回答。',
  // 2026-10-30 學校電腦試裝的回饋：回答太技術，一般使用者照著做不來
  '使用的人多半不懂電腦。回答時：',
  '- 不要用 WSL、PostgreSQL、Redis、連接埠、.env、log、Python 環境這類術語；要提到時換成「萊問的資料庫」「設定」「紀錄」這種說法。',
  '- 不要叫使用者打指令或改設定檔；只講要按哪個按鈕、打開哪個資料夾、刪掉哪個資料夾。',
  '- 一次只講一個步驟，講清楚按完會看到什麼。',
  '- 需要系統管理員權限、或使用者自己處理不了的事，就請他找電腦的管理員，並說「把故障診斷裡『給技術人員看』的內容給他」。',
  '',
  '你知道的事：',
  '- 安裝分兩段：setup.exe 先複製檔案（約 26 GB）；第一次打開萊問時自動完成安裝，約 5～10 分鐘。',
  '- 第一次打開時依序：偵測顯示卡選模型（12 GB 以上用 Qwen3-VL 8B、4～12 GB 用 4B、沒有 NVIDIA 顯示卡只能看介面）',
  '  → 確認 WSL2 → 匯入 WSL 環境 artrag（PostgreSQL、Redis、3D 重建的沙箱）→ 寫設定檔 .env → 建 Python 環境',
  '  → 建知識庫索引（約 1～2 分鐘）→ 啟動服務（約 1 分鐘）→ 顯示萊問的畫面。',
  '- 還沒有 WSL2 的電腦：會跳出系統管理員確認視窗，按「是」；完成後要重新開機一次，開機後再打開萊問會接著裝。',
  '- 「BIOS 的虛擬化功能沒有開」：重開機進 BIOS，開啟 Intel VT-x 或 AMD SVM（每台電腦選單名稱不同）。',
  '- 失敗時視窗會列出「故障診斷」：可能的原因與修法；照著處理後按「重試」。',
  '- 右下角系統匣的萊問圖示按右鍵：重新啟動服務、故障診斷、開啟紀錄資料夾、結束萊問（停止全部服務）。',
  '- 關掉視窗不會停止服務（收到系統匣）；用完要釋放記憶體就從系統匣「結束萊問」。',
  '- 紀錄在安裝位置的 app\\data\\logs；設定檔是 app\\.env。',
  '- 裝好預設是展示模式：左下角的身分選單可以不用密碼切換一般員工、倉管、業務、主管、藝術家。',
  '- 第一次執行 setup.exe 或萊問，Windows 可能跳出「Windows 已保護您的電腦」：按「其他資訊」→「仍要執行」。',
].join('\n');

function parseEnv(file) {
  const out = {};
  try {
    for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Za-z_][\w]*)\s*=\s*(.*)$/);
      if (m) out[m[1]] = m[2].trim();
    }
  } catch {
    // 還沒寫 .env（安裝的前幾步）
  }
  return out;
}

// 只准本機或私有網段：紀錄不能送出這台電腦（和 deploy/diagnose.py 的 is_local_url 同一個原則）
function isLocal(url) {
  try {
    const host = new URL(url).hostname.replace(/^\[|\]$/g, '');
    return /^(localhost|127\.\d+\.\d+\.\d+|::1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)$/.test(host);
  } catch {
    return false;
  }
}

function portOpen(host, port) {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port, timeout: 1000 }, () => {
      sock.destroy();
      resolve(true);
    });
    sock.on('timeout', () => {
      sock.destroy();
      resolve(false);
    });
    sock.on('error', () => resolve(false));
  });
}

function postJson(url, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body), 'utf8');
    const req = http.request(
      url,
      { method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': data.length }, timeout: timeoutMs },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          if (res.statusCode !== 200) return reject(new Error(`模型服務回應 ${res.statusCode}：${text.slice(0, 200)}`));
          try {
            resolve(JSON.parse(text));
          } catch (err) {
            reject(err);
          }
        });
      },
    );
    req.on('timeout', () => req.destroy(new Error('等太久沒有回應')));
    req.on('error', reject);
    req.end(data);
  });
}

function createAssistant({ repo, offlineDir, readJson, log }) {
  const isBundle = fs.existsSync(path.join(offlineDir, 'offline.json'));
  let starting = null;

  function settings() {
    const env = parseEnv(path.join(repo, '.env'));
    const info = (isBundle && readJson(path.join(offlineDir, 'offline.json'))) || {};
    const baseUrl = env.HYBRID_BASE_URL || `http://127.0.0.1:${info.ollama_port || (isBundle ? 11435 : 11434)}/v1`;
    // 主力模型優先（4B 解釋錯誤常答非所問），叫不到才用備援；顯示卡小的電腦 HYBRID_MODEL 本來就是 4B
    const models = [env.HYBRID_MODEL, env.HYBRID_FALLBACK_MODEL, ...(info.models || [])].filter(Boolean);
    const example = parseEnv(path.join(repo, '.env.example'));
    const secrets = Object.entries(env)
      .filter(([k, v]) => /KEY|SECRET|PASSWORD|TOKEN/i.test(k) && v.length >= 4 && v !== example[k])
      .map(([, v]) => v);
    return { baseUrl, models: [...new Set(models)], secrets, mock: (env.LLM_MODE || '').toLowerCase() === 'mock' };
  }

  async function ensureOllama(baseUrl) {
    const u = new URL(baseUrl);
    const port = Number(u.port) || 80;
    if (await portOpen(u.hostname, port)) return true;
    const exe = path.join(offlineDir, 'ollama', 'ollama.exe');
    if (!isBundle || !['127.0.0.1', 'localhost'].includes(u.hostname) || !fs.existsSync(exe)) return false;
    if (!starting) {
      starting = (async () => {
        log('（AI 助理：模型服務還沒啟動，先開離線包的 Ollama）');
        // 不給標準輸入：子行程不能掛在繼承來的管線上（見 runPowerShell 的說明）
        const child = spawn(exe, ['serve'], {
          env: { ...process.env, OLLAMA_HOST: `127.0.0.1:${port}`, OLLAMA_MODELS: path.join(offlineDir, 'ollama-models') },
          stdio: 'ignore',
          windowsHide: true,
          detached: true,
        });
        child.unref();
        const deadline = Date.now() + OLLAMA_START_TIMEOUT_MS;
        while (Date.now() < deadline) {
          if (await portOpen('127.0.0.1', port)) return true;
          await new Promise((r) => setTimeout(r, 1000));
        }
        return false;
      })().finally(() => {
        starting = null;
      });
    }
    return starting;
  }

  // context：{ title, detail, lines }；回傳 { answer } 或 { error }
  async function ask(question, context) {
    const q = String(question || '').trim().slice(0, 300);
    if (!q) return { error: '請輸入問題' };
    const s = settings();
    if (!isLocal(s.baseUrl)) return { error: '模型位址不是這台電腦或內網，紀錄不送出' };
    if (!(await ensureOllama(s.baseUrl))) return { error: '模型服務還沒準備好，請稍後再問一次' };
    const all = context.lines || [];
    const at = all.lastIndexOf(DIAG_MARK);
    const diagnosis = at >= 0 ? all.slice(at + 1) : [];
    const before = at >= 0 ? all.slice(0, at) : all;
    const errors = before.filter((l) => ERRORISH.test(l)).slice(-ERROR_LINES);
    const mask = (lines) => {
      let text = lines.join('\n');
      for (const secret of s.secrets) text = text.split(secret).join('***');
      return text;
    };
    const user = [
      `【目前狀態】${context.title || ''}${context.detail ? `：${context.detail}` : ''}`,
      diagnosis.length ? `【故障診斷的結論】\n${mask(diagnosis)}` : '',
      errors.length ? `【紀錄裡的錯誤】\n${mask(errors)}` : '',
      `【最近的紀錄】\n${mask(before.slice(-LOG_LINES)) || '（還沒有）'}`,
      `【使用者的問題】${q}`,
    ]
      .filter(Boolean)
      .join('\n\n');
    const root = s.baseUrl.replace(/\/v1\/?$/, '');
    const reasons = [];
    for (const model of s.models.length ? s.models : ['qwen3-vl:4b-instruct']) {
      try {
        // Ollama 原生 API：keep_alive 設短一點，問完很快就把顯示記憶體還給建索引和主力模型
        const data = await postJson(
          `${root}/api/chat`,
          {
            model,
            messages: [
              { role: 'system', content: SYSTEM_PROMPT },
              { role: 'user', content: user },
            ],
            stream: false,
            keep_alive: '1m',
            options: { temperature: 0.2, num_predict: 400 },
          },
          ANSWER_TIMEOUT_MS,
        );
        const answer = String(data?.message?.content || '')
          .replace(/<think>[\s\S]*?<\/think>/g, '')
          .trim();
        if (answer) return { answer, model, slow: s.mock };
        reasons.push(`${model}：沒有內容`);
      } catch (err) {
        reasons.push(`${model}：${err.message}`);
      }
    }
    return { error: `AI 助理現在叫不到模型（${reasons.join('；')}）` };
  }

  return { ask };
}

module.exports = { createAssistant, isLocal };
