// 萊問（art-rag 桌面程式）的主行程（docs/adr/047）。
// 介面就是後端在 :8000 提供的網頁（frontend/ 不用改）；這裡負責第一次安裝、起停服務、系統匣，
// 真正的安裝與啟動都交給既有的 PowerShell 腳本（install-offline.ps1、start-services.ps1），不另寫一套。
const { app, BrowserWindow, Menu, Tray, ipcMain, nativeImage, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { StringDecoder } = require('string_decoder');

const APP_URL = 'http://127.0.0.1:8000';
const APP_ORIGIN = new URL(APP_URL).origin;
// install-offline.ps1 啟用 WSL2 之後用這個結束碼表示「要重新開機」（和 Windows Installer 的 3010 同義）
const REBOOT_EXIT = 3010;

// 安裝後的結構：<安裝位置>\desktop\LaiWen.exe、<安裝位置>\app（程式）、<安裝位置>\offline（離線包）。
// 開發時（npm start）直接用這個 repo
const repo = app.isPackaged
  ? path.resolve(path.dirname(process.execPath), '..', 'app')
  : path.resolve(__dirname, '..');
const offlineInfoPath = path.resolve(repo, '..', 'offline', 'offline.json');
const isBundle = fs.existsSync(offlineInfoPath);
const logsDir = path.join(repo, 'data', 'logs');
const installedPath = path.join(repo, 'data', 'installed.json');
const startScript = path.join(repo, 'deploy', 'windows', 'start-services.ps1');
const installScript = path.join(repo, 'deploy', 'offline', 'install-offline.ps1');

let win = null;
let tray = null;
let busy = false;
let quitting = false;
let showingApp = false;
let toldAboutTray = false;
let state = { kind: 'working', title: '準備中' };
const logLines = [];

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
  } catch {
    return null;
  }
}

// 換了新版離線包（commit 不同）也要重跑安裝：.env、Python 環境、索引都可能要跟著更新
function isInstalled() {
  const installed = readJson(installedPath);
  const info = readJson(offlineInfoPath);
  return Boolean(installed && info && installed.commit === info.commit);
}

function log(line) {
  logLines.push(line);
  if (logLines.length > 500) logLines.shift();
  try {
    fs.mkdirSync(logsDir, { recursive: true });
    fs.appendFileSync(path.join(logsDir, 'desktop.log'), `${new Date().toISOString()} ${line}\n`);
  } catch {
    // 紀錄寫不進去不影響使用
  }
  if (win && !showingApp) win.webContents.send('log', line);
}

function setState(next) {
  state = next;
  if (win && !showingApp) win.webContents.send('state', state);
  updateTrayMenu();
}

function backendUp() {
  return new Promise((resolve) => {
    const req = http.get(`${APP_URL}/api/v1/health`, { timeout: 3000 }, (res) => {
      res.resume();
      resolve(true);
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

// 在背景跑一段 PowerShell，逐行回報輸出；回傳結束碼與是否有 ✗。
// 用 -EncodedCommand 避開命令列的引號問題；輸出強制 UTF-8（預設是系統的 OEM 字碼頁，英文版 Windows 會把中文變成 ?）
function runPowerShell(body) {
  const script = [
    '[Console]::OutputEncoding = [Text.Encoding]::UTF8',
    // 沒有主控台時 PowerShell 會把進度條序列化成 CLIXML 寫到 stderr，紀錄裡全是雜訊
    "$ProgressPreference = 'SilentlyContinue'",
    '$global:LASTEXITCODE = 0',
    `try { ${body}; if ($LASTEXITCODE -eq ${REBOOT_EXIT}) { exit ${REBOOT_EXIT} }; exit 0 }`,
    "catch { Write-Output ('✗ ' + $_.Exception.Message); exit 1 }",
  ].join('\n');
  const encoded = Buffer.from(script, 'utf16le').toString('base64');
  return new Promise((resolve) => {
    let problems = false;
    const child = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
      {
        cwd: repo,
        windowsHide: true,
        // 不給標準輸入：背景的 wsl.exe（帶著 PostgreSQL／Redis）會一直讀繼承來的輸入管線，
        // 之後起的 python 初始化時查這條管線就會卡死（建索引停在 0% CPU）
        stdio: ['ignore', 'pipe', 'pipe'],
        // 診斷（Python）的輸出直接寫到這條管線：也要 UTF-8
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      },
    );
    const pump = (stream) => {
      const decoder = new StringDecoder('utf8');
      let rest = '';
      stream.on('data', (chunk) => {
        const lines = (rest + decoder.write(chunk)).split(/\r?\n/);
        rest = lines.pop();
        for (const line of lines) {
          if (/^\s*✗/.test(line)) problems = true;
          if (line.trim()) log(line);
        }
      });
      stream.on('end', () => {
        if (rest.trim()) log(rest);
      });
    };
    pump(child.stdout);
    pump(child.stderr);
    let done = false;
    const finish = (code) => {
      if (done) return;
      done = true;
      // start-services.ps1 起的服務會繼承管線，管線不會關：結束後就不再讀
      child.stdout.destroy();
      child.stderr.destroy();
      resolve({ code: code ?? 1, problems });
    };
    child.on('close', finish);
    // 等一下讓最後幾行讀完
    child.on('exit', (code) => setTimeout(() => finish(code), 1500));
    child.on('error', (err) => {
      log(`✗ 無法執行 PowerShell：${err.message}`);
      finish(1);
    });
  });
}

const psQuote = (s) => `'${s.replace(/'/g, "''")}'`;
const runScript = (file, args = []) => runPowerShell(`& ${psQuote(file)} ${args.join(' ')}`);

function showSplash() {
  showingApp = false;
  win.loadFile(path.join(__dirname, 'splash.html'));
  showWindow();
}

function openApp() {
  busy = false;
  showingApp = true;
  setState({ kind: 'ready', title: '執行中' });
  win.loadURL(APP_URL);
  showWindow();
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

const BUTTONS = {
  retry: { id: 'retry', label: '重試', primary: true },
  openAnyway: { id: 'open-anyway', label: '仍要開啟', primary: true },
  logs: { id: 'open-logs', label: '開啟紀錄資料夾' },
  quit: { id: 'quit', label: '結束萊問' },
};

async function boot() {
  if (busy) return showWindow();
  busy = true;
  logLines.length = 0;
  showSplash();
  setState({ kind: 'working', title: '確認服務' });
  if (await backendUp()) return openApp();

  if (isBundle && !isInstalled()) {
    setState({ kind: 'working', title: '第一次使用：安裝中', detail: '約 5～10 分鐘，請不要關機' });
    const r = await runScript(installScript, ['-NoStart', '-NoShortcut']);
    if (r.code === REBOOT_EXIT) {
      busy = false;
      return setState({
        kind: 'reboot',
        title: '請重新開機',
        detail: 'WSL2 已啟用。重新開機之後，再打開萊問就會繼續安裝。',
        buttons: [BUTTONS.quit],
      });
    }
    if (r.code !== 0) {
      busy = false;
      return setState({
        kind: 'error',
        title: '安裝沒有完成',
        detail: '下面的紀錄最後一段是「故障診斷」，列出可能的原因與修法。照著處理後按「重試」。',
        buttons: [BUTTONS.retry, BUTTONS.logs, BUTTONS.quit],
      });
    }
  }

  setState({ kind: 'working', title: '啟動服務', detail: '約 1 分鐘' });
  const r = await runScript(startScript);
  busy = false;
  if (r.code !== 0 || !(await backendUp())) {
    return setState({
      kind: 'error',
      title: '服務沒有啟動',
      detail: '下面的紀錄最後一段是「故障診斷」，列出可能的原因與修法。照著處理後按「重試」。',
      buttons: [BUTTONS.retry, BUTTONS.logs, BUTTONS.quit],
    });
  }
  if (r.problems) {
    return setState({
      kind: 'warning',
      title: '已經啟動，但有服務不正常',
      detail: '標 ✗ 的功能可能不能用；紀錄最後有「故障診斷」。',
      buttons: [BUTTONS.openAnyway, BUTTONS.retry, BUTTONS.logs],
    });
  }
  return openApp();
}

async function restartServices() {
  if (busy) return showWindow();
  busy = true;
  showSplash();
  setState({ kind: 'working', title: '停止服務' });
  await runScript(startScript, ['-Stop']);
  busy = false;
  await boot();
}

async function diagnose() {
  if (busy) return showWindow();
  busy = true;
  logLines.length = 0;
  showSplash();
  setState({ kind: 'working', title: '故障診斷', detail: '比對最近的紀錄' });
  // diagnose.py 要用離線包的 uv 與 Python：先載入 offline-env.ps1（沒有離線包時它什麼都不做）
  await runPowerShell(
    [
      `$Repo = ${psQuote(repo)}`,
      'Set-Location $Repo',
      ". (Join-Path $Repo 'deploy\\offline\\offline-env.ps1')",
      'uv run --no-project python deploy\\diagnose.py',
    ].join('; '),
  );
  busy = false;
  const up = await backendUp();
  setState({
    kind: 'info',
    title: '故障診斷結果',
    detail: up ? '服務正在執行。' : '服務沒有在執行。',
    buttons: [up ? { ...BUTTONS.openAnyway, label: '回到萊問' } : BUTTONS.retry, BUTTONS.logs],
  });
}

async function quitAll() {
  if (quitting) return;
  quitting = true;
  showSplash();
  setState({ kind: 'working', title: '停止服務', detail: '釋放顯示卡與記憶體' });
  await runScript(startScript, ['-Stop']);
  if (tray) tray.destroy();
  app.exit(0);
}

function updateTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: '開啟萊問', click: showWindow },
      { type: 'separator' },
      { label: '重新啟動服務', enabled: !busy && !quitting, click: restartServices },
      { label: '故障診斷', enabled: !busy && !quitting, click: diagnose },
      { label: '開啟紀錄資料夾', click: () => shell.openPath(logsDir) },
      { type: 'separator' },
      { label: '結束萊問（停止全部服務）', enabled: !quitting, click: quitAll },
    ]),
  );
}

function isAppUrl(url) {
  try {
    return new URL(url).origin === APP_ORIGIN;
  } catch {
    return false;
  }
}

function openExternal(url) {
  // 只交出 http(s)：其他協定（file:、自訂協定）不開
  if (/^https?:\/\//i.test(url)) shell.openExternal(url);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: '萊問',
    icon: path.join(__dirname, 'build', 'icon.png'),
    backgroundColor: '#121212',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.once('ready-to-show', () => win.show());
  // 視窗標題固定是程式名稱，不跟著網頁的 <title> 變
  win.on('page-title-updated', (event) => event.preventDefault());
  win.webContents.on('did-finish-load', () => {
    if (showingApp) return;
    for (const line of logLines) win.webContents.send('log', line);
    win.webContents.send('state', state);
  });
  // 網頁介面的新視窗（例如開圖片）：本機的留在程式裡，外部連結交給預設瀏覽器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isAppUrl(url)) return { action: 'allow', overrideBrowserWindowOptions: { autoHideMenuBar: true } };
    openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (isAppUrl(url) || url.startsWith('file:')) return;
    event.preventDefault();
    openExternal(url);
  });
  // 關視窗只是收到系統匣，服務繼續跑；要停止從系統匣「結束」
  win.on('close', (event) => {
    if (quitting) return;
    event.preventDefault();
    win.hide();
    if (!toldAboutTray && tray) {
      toldAboutTray = true;
      tray.displayBalloon({
        iconType: 'info',
        title: '萊問仍在背景執行',
        content: '要停止服務、釋放記憶體：在系統匣的萊問圖示按右鍵 →「結束萊問」。',
      });
    }
  });
}

ipcMain.on('action', (_event, name) => {
  if (name === 'retry') boot();
  else if (name === 'open-anyway') openApp();
  else if (name === 'open-logs') shell.openPath(logsDir);
  else if (name === 'quit') quitAll();
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);
  app.whenReady().then(() => {
    app.setAppUserModelId('LaiWen');
    Menu.setApplicationMenu(null);
    tray = new Tray(nativeImage.createFromPath(path.join(__dirname, 'build', 'tray.png')));
    tray.setToolTip('萊問');
    tray.on('click', showWindow);
    updateTrayMenu();
    createWindow();
    boot();
  });
  // 視窗只會隱藏、不會真的關：留在系統匣
  app.on('window-all-closed', () => {});
}
