; Per-user Windows installer. Campaign data lives in LocalAppData and is never
; removed by this installer or its uninstaller.

#define AppName "SOLAR RPG"
#define AppVersion "0.1.1"
#define AppPublisher "SOLAR RPG Team"
#define AppExeName "SOLAR RPG.exe"

[Setup]
AppId={{5B370F64-BDEC-432A-9EB0-A4B52E4A55E8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=SOLAR-RPG-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}

[Files]
Source: "..\dist\SOLAR RPG\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Deliberately omit LocalAppData\SOLARRPG: worlds, saves, cache, and settings
; survive upgrades and uninstall unless the player removes them manually.
