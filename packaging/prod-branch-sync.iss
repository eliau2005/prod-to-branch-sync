; Inno Setup script — builds the self-contained Windows installer.
; Compiled by packaging/build.ps1, which stages everything into ..\dist\app first.

#define AppName "Prod Branch Sync"
#ifndef AppVersion
  #define AppVersion "1.0.1"
#endif
#define AppPublisher "eliau2005"
#ifndef SourceDir
  #define SourceDir "..\dist\app"
#endif

[Setup]
AppId={{B7E6B2B0-1C3A-4F5E-9D21-PRODBRANCHSYNC}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\ProdBranchSync
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install (no admin) so the app can write its .env config at runtime.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ProdBranchSync-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=app.ico
UninstallDisplayIcon={app}\app.ico

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\launch.vbs"; Description: "Launch {#AppName} now"; Flags: shellexec nowait postinstall skipifsilent

[UninstallDelete]
; Remove runtime-created files so uninstall leaves nothing behind.
Type: files; Name: "{app}\.env"
Type: filesandordirs; Name: "{app}\pgsql"
