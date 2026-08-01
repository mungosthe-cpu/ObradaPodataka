; Podrazumevano puno ime; za Lite: ISCC /DAppDisplayName=TaurisPdfApp Lite
#ifndef AppDisplayName
#define AppDisplayName "TaurisPdfApp"
#endif
; Izlazni instalacioni fajl: ISCC /DSetupBaseFilename=TaurisPdfApp_Lite_Setup
#ifndef SetupBaseFilename
#define SetupBaseFilename "TaurisPdfApp_Setup"
#endif
; Lite: ISCC /DIncludeClubEmail — kopira tauris_club_email.json pored .exe (SMTP)

[Setup]
AppId={{7C46AB8C-9C08-4B70-9202-52D4B275EA10}}
AppName={#AppDisplayName}
AppVersion=1.7.2
AppPublisher=Dragan Todorovic
SetupIconFile=ICON\app.ico
UninstallDisplayIcon={app}\TaurisPdfApp.exe
UninstallDisplayName={#AppDisplayName}
PrivilegesRequired=admin
UsePreviousAppDir=yes
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
DefaultDirName={autopf}\TaurisPdfApp
DefaultGroupName={#AppDisplayName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename={#SetupBaseFilename}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"
Name: "briconusbdriver"; Description: "Instaliraj BRICON USB drajver (FTDI, preporuceno za BRICON sat)"; GroupDescription: "BRICON USB:"; Flags: checkedonce

[Files]
Source: "dist\TaurisPdfApp.exe"; DestDir: "{app}"; Flags: ignoreversion
#ifdef IncludeClubEmail
Source: "tauris_club_email.json"; DestDir: "{app}"; Flags: ignoreversion
#endif
; BRICON USB drajveri (preuzeti skriptom scripts\fetch_bricon_drivers.ps1 pre kompajliranja)
Source: "third_party\bricon\drivers\CDM_Setup.exe"; DestDir: "{app}\drivers"; Flags: ignoreversion
Source: "third_party\bricon\drivers\USBDriver_win7\USBDriverInstallerV2.04.16.exe"; DestDir: "{app}\drivers"; DestName: "USBDriverInstaller_win7.exe"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppDisplayName}"; Filename: "{app}\TaurisPdfApp.exe"
Name: "{autoprograms}\Uninstall {#AppDisplayName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppDisplayName}"; Filename: "{app}\TaurisPdfApp.exe"; Tasks: desktopicon

[Run]
; CDM_Setup (FTDI) ne prihvata /S — prikazuje se zvanični FTDI čarobnjak (Next/Finish).
Filename: "{app}\drivers\CDM_Setup.exe"; Description: "BRICON USB drajver (FTDI)"; StatusMsg: "BRICON USB drajver (FTDI) — pratite prozor i kliknite Next"; Flags: waituntilterminated; Check: ShouldInstallBriconCdmDriver
Filename: "{app}\drivers\USBDriverInstaller_win7.exe"; Description: "BRICON USB drajver (Win7)"; StatusMsg: "BRICON USB drajver — pratite uputstva na ekranu"; Flags: waituntilterminated; Check: ShouldInstallBriconLegacyDriver
Filename: "{app}\TaurisPdfApp.exe"; Description: "Launch TaurisPdfApp"; Flags: nowait postinstall skipifsilent

[Code]
function BriconDriverTaskSelected: Boolean;
begin
  Result := WizardIsTaskSelected('briconusbdriver');
end;

function ShouldInstallBriconCdmDriver: Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major >= 10) and BriconDriverTaskSelected;
end;

function ShouldInstallBriconLegacyDriver: Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major < 10) and BriconDriverTaskSelected;
end;

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
