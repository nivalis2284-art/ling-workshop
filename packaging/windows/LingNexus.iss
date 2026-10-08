; Official reference: https://jrsoftware.org/ishelp/
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef BundleDir
  #define BundleDir "..\dist\LingNexus"
#endif
#ifndef InstallerOutput
  #define InstallerOutput "..\installer"
#endif

[Setup]
AppId={{051207B2-6843-4D0F-951E-FB21F59D4C17}
AppName=LingNexus
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\LingNexus
DefaultGroupName=LingNexus
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#InstallerOutput}
OutputBaseFilename=LingNexus-{#AppVersion}-Setup
SetupIconFile=LingNexus.ico
UninstallDisplayIcon={app}\LingNexus.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
UsePreviousTasks=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
Name: "autostart"; Description: "登录 Windows 时在后台启动 LingNexus"; Flags: unchecked

[Dirs]
; Data is outside the application directory and never belongs to the uninstaller.
Name: "{localappdata}\LingNexus"; Flags: uninsneveruninstall

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LingNexus"; Filename: "{app}\LingNexus.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\LingNexus"; Filename: "{app}\LingNexus.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LingNexus"; ValueData: """{app}\LingNexus.exe"" --background --data-dir ""{localappdata}\LingNexus"" --port 8765"; Tasks: autostart

[Run]
Filename: "{app}\LingNexus.exe"; Description: "打开 LingNexus"; Flags: nowait postinstall skipifsilent

[Code]
function StopInstalledInstance(): Boolean;
var
  ExitCode: Integer;
  Exe: String;
begin
  Exe := ExpandConstant('{app}\LingNexus.exe');
  Result := True;
  if FileExists(Exe) then
    Result := Exec(Exe, '--quit', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopInstalledInstance() then
    Result := 'LingNexus 仍在运行，请从托盘菜单退出后重试安装。';
end;

function InitializeUninstall(): Boolean;
begin
  Result := StopInstalledInstance();
  if not Result then
    MsgBox('LingNexus 仍在运行，请从托盘菜单退出后重试卸载。', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
  Prefix: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Prefix := '"' + ExpandConstant('{app}\LingNexus.exe') + '"';
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'LingNexus', Command)
      and (CompareText(Copy(Command, 1, Length(Prefix)), Prefix) = 0) then
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'LingNexus');
  end;
  // No database, attachment, workspace or backup deletion on uninstall.
end;
