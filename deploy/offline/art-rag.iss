; 萊問（art-rag 桌面程式）安裝精靈（docs/adr/047）：deploy\offline\pack.ps1 用 Inno Setup 6 編譯成 setup.exe。
; 約 25 GB 的資料不壓進 exe：放在 setup.exe 旁邊的 payload\，安裝時直接複製（external）。
; 模型本來就壓不小，這樣打包快，也不會有單一安裝檔太大的問題。
; 真正的安裝（WSL 環境、Python 環境、索引）在第一次打開萊問時由 install-offline.ps1 做，進度顯示在程式視窗裡。

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef Commit
  #define Commit "dev"
#endif
; payload 的大小（位元組）＋安裝後再用的空間：external 的檔案編譯時不算大小，要自己告訴安裝精靈
#ifndef ExtraBytes
  #define ExtraBytes "37000000000"
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
AppId={{0618A7A7-C1AB-43E2-99ED-1773859D3F78}
AppName=萊問
AppVersion={#AppVersion}
AppVerName=萊問 {#AppVersion}（{#Commit}）
AppPublisher=LAIWEN
VersionInfoVersion={#AppVersion}
; 預設不放 Program Files：程式執行時要在安裝資料夾裡寫 .env、紀錄、Python 環境與 WSL 的虛擬磁碟，
; 一般使用者權限就能裝，不用跳系統管理員確認
; 資料夾名稱用英文：uv、WSL、PostgreSQL 的路徑都經過它
DefaultDirName={sd}\LaiWen
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=setup
SetupIconFile=..\..\desktop\build\icon.ico
UninstallDisplayIcon={app}\desktop\LaiWen.exe
UninstallDisplayName=萊問
ExtraDiskSpaceRequired={#ExtraBytes}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 21H2：WSL2 的 localhost 轉送與離線 msi 都需要
MinVersion=10.0.19044
SetupLogging=yes

[Languages]
Name: "zh"; MessagesFile: "ChineseTraditional.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面建立萊問捷徑"

[Files]
; 有人直接從 payload 打開過程式（就地安裝）時，會在裡面留下這台電腦的設定、Python 環境、紀錄：不帶到別台。
; 不加 createallsubdirs：它會照樣建出被排除的資料夾結構，空的 backend\.venv 會讓 uv 拒絕使用（git archive 本來就沒有空資料夾）
Source: "{src}\payload\app\*"; DestDir: "{app}\app"; Excludes: "\.env,\backend\.venv,\data,\scheduler\.java-home"; Flags: external recursesubdirs ignoreversion
Source: "{src}\payload\offline\*"; DestDir: "{app}\offline"; Flags: external recursesubdirs createallsubdirs ignoreversion
Source: "{src}\payload\desktop\*"; DestDir: "{app}\desktop"; Flags: external recursesubdirs createallsubdirs ignoreversion
Source: "{src}\使用說明.txt"; DestDir: "{app}"; Flags: external ignoreversion

[Icons]
Name: "{autoprograms}\萊問"; Filename: "{app}\desktop\LaiWen.exe"; WorkingDir: "{app}\desktop"; AppUserModelID: "LaiWen"
Name: "{autodesktop}\萊問"; Filename: "{app}\desktop\LaiWen.exe"; WorkingDir: "{app}\desktop"; AppUserModelID: "LaiWen"; Tasks: desktopicon

[Run]
Filename: "{app}\desktop\LaiWen.exe"; Description: "立即開啟萊問（第一次會先完成安裝，約 5～10 分鐘）"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\app\deploy\offline\uninstall.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "artrag-cleanup"

[UninstallDelete]
; 安裝後才產生的東西（.env、紀錄、Python 環境、WSL 的虛擬磁碟）解除安裝時一起刪
Type: filesandordirs; Name: "{app}"

[Code]
var
  JevPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  JevPage := CreateInputQueryPage(wpSelectDir, 'Jev API key（選填）', '要不要用 Jev 判斷提問的意圖？',
    'Jev 是 TypeSafe 的雲端服務：要連網，只會收到代號化的文字（docs/adr/011）。' + #13#10 +
    '不填也能用：改由這台電腦上的本地路由判斷，其他功能照常。' + #13#10 +
    '之後要補，在安裝資料夾的 app\.env 填 JEV_API_KEY，再從系統匣「重新啟動服務」。' + #13#10#13#10 +
    '已經裝過、這次留空：保留原本的設定。');
  JevPage.Add('API key：', True);
end;

// key 只會是一般的英數符號：擋掉中文、空白等貼錯的內容，寫進 .env 才不會壞掉
function IsPlainKey(Key: String): Boolean;
var
  I: Integer;
begin
  Result := True;
  for I := 1 to Length(Key) do
    if (Ord(Key[I]) < 33) or (Ord(Key[I]) > 126) then
    begin
      Result := False;
      Exit;
    end;
end;

// 用原始位元組改 .env：裡面有中文註解，逐行讀寫會被轉成系統字碼頁
function WriteJevKey(Key: String): Boolean;
var
  EnvFile: String;
  Text: AnsiString;
  P, E: Integer;
begin
  Result := False;
  EnvFile := ExpandConstant('{app}\app\.env');
  // 第一次裝還沒有 .env：和 configure_env.py 一樣從 .env.example 複製，它之後只補自己要的設定，不會動這個 key
  if not FileExists(EnvFile) then
    if not CopyFile(ExpandConstant('{app}\app\.env.example'), EnvFile, True) then Exit;
  if not LoadStringFromFile(EnvFile, Text) then Exit;
  P := Pos(#10 + 'JEV_API_KEY=', Text);
  if P = 0 then
    Text := Text + #10 + 'JEV_API_KEY=' + AnsiString(Key) + #10
  else
  begin
    P := P + Length(#10 + 'JEV_API_KEY=');
    E := P;
    while (E <= Length(Text)) and (Text[E] <> #10) and (Text[E] <> #13) do
      E := E + 1;
    Delete(Text, P, E - P);
    Insert(AnsiString(Key), Text, P);
  end;
  Result := SaveStringToFile(EnvFile, Text, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Key: String;
begin
  if CurStep <> ssPostInstall then Exit;
  Key := Trim(JevPage.Values[0]);
  if (Key <> '') and not WriteJevKey(Key) then
    MsgBox('Jev API key 沒有寫進設定檔，請在 ' + ExpandConstant('{app}\app\.env') + ' 手動填 JEV_API_KEY。', mbError, MB_OK);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not FileExists(ExpandConstant('{src}\payload\offline\offline.json')) then
  begin
    MsgBox('找不到安裝資料：setup.exe 要和旁邊的 payload 資料夾放在一起（整個資料夾一起複製）。', mbCriticalError, MB_OK);
    Result := False;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir, Src: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := AddBackslash(Lowercase(WizardDirValue));
    Src := AddBackslash(Lowercase(ExpandConstant('{src}')));
    if Pos(Src, Dir) = 1 then
    begin
      MsgBox('不能裝在安裝檔所在的資料夾裡，請選別的位置（例如 C:\LaiWen 或 D:\LaiWen）。', mbError, MB_OK);
      Result := False;
    end
    else if Copy(Dir, 1, 2) = '\\' then
    begin
      MsgBox('請裝在這台電腦的本機磁碟，不要裝在網路磁碟。', mbError, MB_OK);
      Result := False;
    end;
  end
  else if CurPageID = JevPage.ID then
  begin
    if not IsPlainKey(Trim(JevPage.Values[0])) then
    begin
      MsgBox('API key 裡有空白或中文等字元，請確認貼上的內容。', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

// 覆蓋安裝新版之前：先關掉程式、停止服務，檔案才不會被佔用
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Script: String;
  Code: Integer;
begin
  Result := '';
  Script := ExpandConstant('{app}\app\deploy\offline\uninstall.ps1');
  if FileExists(Script) then
    Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -StopOnly', '',
      SW_HIDE, ewWaitUntilTerminated, Code);
end;
