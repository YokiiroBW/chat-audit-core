#define AppName "Chat Audit QQ Collector"
#define AppVersion "0.2.0"
#define AppPublisher "Chat Audit Core"
#define AppGuiExeName "chat-audit-qq-collector-gui.exe"
#define AppCliExeName "chat-audit-qq-collector.exe"

[Setup]
AppId={{A4C75D38-30B3-4FD8-AD72-430D8C910E40}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Chat Audit QQ Collector
DefaultGroupName={#AppName}
OutputDir=..\..\dist\installer
OutputBaseFilename=chat-audit-qq-collector-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppGuiExeName}

[Tasks]
Name: "startup"; Description: "登录 Windows 后自动启动 Collector"; GroupDescription: "启动选项:"; Flags: unchecked

[Dirs]
Name: "{localappdata}\ChatAuditQQCollector"

[Files]
Source: "..\..\dist\chat-audit-qq-collector-gui.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\chat-audit-qq-collector.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Chat Audit QQ Collector"; Filename: "{app}\{#AppGuiExeName}"
Name: "{group}\命令行状态"; Filename: "{app}\{#AppCliExeName}"; Parameters: "--config ""{localappdata}\ChatAuditQQCollector\collector.toml"" status"
Name: "{group}\生成故障诊断包"; Filename: "{app}\{#AppCliExeName}"; Parameters: "--config ""{localappdata}\ChatAuditQQCollector\collector.toml"" diagnose"
Name: "{userstartup}\Chat Audit QQ Collector"; Filename: "{app}\{#AppGuiExeName}"; Parameters: "--start-hidden"; Tasks: startup

[Run]
Filename: "{app}\{#AppGuiExeName}"; Description: "启动 Chat Audit QQ Collector"; Flags: postinstall nowait skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if (not UninstallSilent) and
       (MsgBox('是否删除 Collector 配置、本地游标、日志和待上传队列？选择“否”可供以后重装后继续同步。', mbConfirmation, MB_YESNO) = IDYES) then
      DelTree(ExpandConstant('{localappdata}\ChatAuditQQCollector'), True, True, True);
  end;
end;
