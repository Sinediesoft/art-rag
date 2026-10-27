; art-rag 安裝精靈（docs/adr/047）：deploy\offline\pack.ps1 用 Inno Setup 6 編譯成 setup.exe。
; 約 25 GB 的資料不壓進 exe：放在 setup.exe 旁邊的 payload\，安裝時直接複製（external）。
; 模型本來就壓不小，這樣打包快，也不會有單一安裝檔太大的問題。
; 真正的安裝（WSL 環境、Python 環境、索引）在第一次打開 art-rag 時由 install-offline.ps1 做，進度顯示在程式視窗裡。

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
AppName=art-rag
AppVersion={#AppVersion}
AppVerName=art-rag {#AppVersion}（{#Commit}）
AppPublisher=art-rag
VersionInfoVersion={#AppVersion}
; 預設不放 Program Files：程式執行時要在安裝資料夾裡寫 .env、紀錄、Python 環境與 WSL 的虛擬磁碟，
; 一般使用者權限就能裝，不用跳系統管理員確認
DefaultDirName={sd}\art-rag
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=setup
SetupIconFile=..\..\desktop\build\icon.ico
UninstallDisplayIcon={app}\desktop\art-rag.exe
UninstallDisplayName=art-rag
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
Name: "desktopicon"; Description: "在桌面建立 art-rag 捷徑"

[Files]
Source: "{src}\payload\app\*"; DestDir: "{app}\app"; Flags: external recursesubdirs createallsubdirs ignoreversion
Source: "{src}\payload\offline\*"; DestDir: "{app}\offline"; Flags: external recursesubdirs createallsubdirs ignoreversion
Source: "{src}\payload\desktop\*"; DestDir: "{app}\desktop"; Flags: external recursesubdirs createallsubdirs ignoreversion
Source: "{src}\使用說明.txt"; DestDir: "{app}"; Flags: external ignoreversion

[Icons]
Name: "{autoprograms}\art-rag"; Filename: "{app}\desktop\art-rag.exe"; WorkingDir: "{app}\desktop"; AppUserModelID: "art-rag"
Name: "{autodesktop}\art-rag"; Filename: "{app}\desktop\art-rag.exe"; WorkingDir: "{app}\desktop"; AppUserModelID: "art-rag"; Tasks: desktopicon

[Run]
Filename: "{app}\desktop\art-rag.exe"; Description: "立即開啟 art-rag（第一次會先完成安裝，約 5～10 分鐘）"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\app\deploy\offline\uninstall.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "artrag-cleanup"

[UninstallDelete]
; 安裝後才產生的東西（.env、紀錄、Python 環境、WSL 的虛擬磁碟）解除安裝時一起刪
Type: filesandordirs; Name: "{app}"

[Code]
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
      MsgBox('不能裝在安裝檔所在的資料夾裡，請選別的位置（例如 C:\art-rag 或 D:\art-rag）。', mbError, MB_OK);
      Result := False;
    end
    else if Copy(Dir, 1, 2) = '\\' then
    begin
      MsgBox('請裝在這台電腦的本機磁碟，不要裝在網路磁碟。', mbError, MB_OK);
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
