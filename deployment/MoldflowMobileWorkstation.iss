; deployment/MoldflowMobileWorkstation.iss
; --------------------------------------
; Inno Setup Definition for Moldflow Mobile Workstation Onboarding Installer
; Optional standalone GUI executable packaging for IT administrators.

#define MyAppName "Moldflow Mobile Workstation"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "UnoTEAM / Moldflow Mobile"
#define MyAppURL "https://github.com/BhanuSandeep-123/MoldflowMobilePlugin"
#define MyAppExeName "standalone_job_monitor.py"

[Setup]
AppId={{5E73F578-8314-41A8-8F0E-92167FA31A8B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={commonpf}\MoldflowMobileWorkstation
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=MoldflowMobileWorkstationSetup
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Distribute validated workstation runtime files
Source: "C:\Users\UnoTEAM-0144\Documents\MoldflowMobileWorkstation_Staging\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Include installation automation scripts
Source: "Install-MoldflowWorkstation.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "Uninstall-MoldflowWorkstation.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion

[Code]
var
  ConfigPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  // Create Custom Configuration Page for IT Onboarding
  ConfigPage := CreateInputQueryPage(wpSelectDir,
    'Moldflow Mobile Workstation Onboarding',
    'Workstation Identity and Authority Token',
    'Please enter the backend URL and the one-time enrollment token issued by your administrator.');

  ConfigPage.Add('Backend Target URL:', False);
  ConfigPage.Add('Target User ID:', False);
  ConfigPage.Add('One-Time Enrollment Token:', True); // Masked input for secret token

  // Default values
  ConfigPage.Values[0] := 'https://moldflowplugin-mobile-app.onrender.com';
  ConfigPage.Values[1] := 'DEV-USER-001';
  ConfigPage.Values[2] := '';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ConfigPage.ID then
  begin
    if Trim(ConfigPage.Values[0]) = '' then
    begin
      MsgBox('Please enter a valid Backend URL.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

[Run]
; Run the PowerShell installation engine with parameters captured from wizard
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\Install-MoldflowWorkstation.ps1"" -InstallDir ""{app}"" -BackendUrl ""{code:GetBackendUrl}"" -UserId ""{code:GetUserId}"" -EnrollmentToken ""{code:GetEnrollmentToken}"" -Silent"; StatusMsg: "Enrolling workstation and configuring Scheduled Tasks..."; Flags: runhidden

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\Uninstall-MoldflowWorkstation.ps1"" -InstallDir ""{app}"" -Silent"; Flags: runhidden

[Code]
function GetBackendUrl(Param: String): String;
begin
  Result := ConfigPage.Values[0];
end;

function GetUserId(Param: String): String;
begin
  Result := ConfigPage.Values[1];
end;

function GetEnrollmentToken(Param: String): String;
begin
  Result := ConfigPage.Values[2];
end;
