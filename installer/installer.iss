; Paper Review Automation — Inno Setup installer script.
;
; Build with (after `py -m PyInstaller installer\paper_review_automation.spec
; --distpath dist --workpath build --noconfirm` has produced dist\PaperReviewAutomation\):
;
;     ISCC.exe installer\installer.iss
;
; Per-user install (PrivilegesRequired=lowest) — no admin rights, no UAC
; prompt. Program files live under %LOCALAPPDATA%\Programs\PaperReviewAutomation;
; user data (config.toml, state\, logs\) lives separately under
; %USERPROFILE%\PaperReviewAutomation — see paper_automation/config.py's
; _default_base_dir(). Uninstall only ever removes the program-files half;
; the data folder is never touched, so re-running a newer installer over an
; existing one (Inno Setup's built-in upgrade-in-place behaviour) replaces
; the exes and leaves accounts/job history/config alone.

#define MyAppName "Paper Review Automation"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "iMatiz"
#define MyAppExeName "PaperReviewAutomation.exe"
#define DistDir "..\dist\PaperReviewAutomation"

[Setup]
AppId={{6E6F5C6D-6F6E-4B0A-9B7F-5B6D1B0C9F1A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\PaperReviewAutomation
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputBaseFilename=PaperReviewAutomation-Setup-{#MyAppVersion}
OutputDir=..\dist\installer
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; Not code-signed (see CLAUDE.md / project plan) — Windows SmartScreen may
; warn on first run until a certificate is added later.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Everything PyInstaller's COLLECT step produced — the five exes plus their
; shared runtime/DLLs and bundled webui templates/static/models.json/config.example.toml.
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Parameters: "--open"; WorkingDir: "{app}"; \
    IconFilename: "{app}\webui\static\tray.ico"; \
    Comment: "Open the Paper Review Automation dashboard"
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Parameters: "--open"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; First-run setup wizard: papers folder, timezone, first admin account.
; "runascurrentuser" is the default for a lowest-privilege install; skipifsilent
; keeps a fully unattended /VERYSILENT install from blocking on a GUI prompt
; (an admin can still run FirstRunSetup.exe by hand afterwards in that case).
Filename: "{app}\FirstRunSetup.exe"; Description: "Run first-time setup"; \
    Flags: postinstall skipifsilent nowait

[UninstallDelete]
; Explicitly scoped to the program-files install directory only — Inno Setup's
; default uninstall already limits itself to {app}, but this is spelled out so
; it's obvious on review that the data folder below is deliberately absent
; from every [UninstallDelete]/[UninstallRun] entry in this script.
Type: filesandordirs; Name: "{app}"

[Code]
const
  TaskNameRun = 'ResearchPaperAutomation';
  TaskNameTray = 'ResearchPaperAutomationTray';

procedure RegisterScheduledTasks();
var
  ResultCode: Integer;
  RunExe, TrayExe, Cmd: String;
begin
  RunExe := ExpandConstant('{app}\paper-review-run.exe');
  TrayExe := ExpandConstant('{app}\PaperReviewAutomation.exe');

  // Daily 09:00 run (Task Scheduler fires in machine local time; the pipeline
  // itself decides "current month" using config.toml's timezone — see
  // scripts/register_task.ps1, which this mirrors for the installed exe).
  Cmd := Format('/Create /F /SC DAILY /ST 09:00 /TN "%s" /TR "\"%s\""', [TaskNameRun, RunExe]);
  Exec(ExpandConstant('{sys}\schtasks.exe'), Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // Logon autostart for the tray icon (mirrors scripts/register_tray_autostart.ps1).
  Cmd := Format('/Create /F /SC ONLOGON /TN "%s" /TR "\"%s\""', [TaskNameTray, TrayExe]);
  Exec(ExpandConstant('{sys}\schtasks.exe'), Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure UnregisterScheduledTasks();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "' + TaskNameRun + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "' + TaskNameTray + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RegisterScheduledTasks();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    UnregisterScheduledTasks();
end;
