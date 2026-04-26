[Setup]
AppId={{7C46AB8C-9C08-4B70-9202-52D4B275EA10}}
AppName=TaurisPdfApp
AppVersion=1.0.1
AppPublisher=Dragan Todorovic
SetupIconFile=ICON\app.ico
UninstallDisplayIcon={app}\TaurisPdfApp.exe
UninstallDisplayName=TaurisPdfApp
PrivilegesRequired=admin
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
DefaultDirName={autopf}\TaurisPdfApp
DefaultGroupName=TaurisPdfApp
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=TaurisPdfApp_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\TaurisPdfApp.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\TaurisPdfApp"; Filename: "{app}\TaurisPdfApp.exe"
Name: "{autoprograms}\Uninstall TaurisPdfApp"; Filename: "{uninstallexe}"
Name: "{autodesktop}\TaurisPdfApp"; Filename: "{app}\TaurisPdfApp.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TaurisPdfApp.exe"; Description: "Launch TaurisPdfApp"; Flags: nowait postinstall skipifsilent

[Code]
procedure KillRunningApp;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM TaurisPdfApp.exe /T', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    KillRunningApp;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    KillRunningApp;
end;
