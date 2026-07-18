#define AppName "Chat Audit QQ Collector"
#define AppVersion "0.1.0"
#define AppPublisher "Chat Audit Core"
#define AppExeName "chat-audit-qq-collector.exe"

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
UninstallDisplayIcon={app}\{#AppExeName}

[Tasks]
Name: "startup"; Description: "登录 Windows 后自动启动 Collector"; GroupDescription: "启动选项:"; Flags: unchecked

[Dirs]
Name: "{localappdata}\ChatAuditQQCollector"

[Files]
Source: "..\..\dist\chat-audit-qq-collector.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\collector.example.toml"; DestDir: "{localappdata}\ChatAuditQQCollector"; DestName: "collector.toml"; Flags: onlyifdoesntexist

[Icons]
Name: "{group}\Collector 状态"; Filename: "{app}\{#AppExeName}"; Parameters: "--config ""{localappdata}\ChatAuditQQCollector\collector.toml"" status"
Name: "{group}\生成故障诊断包"; Filename: "{app}\{#AppExeName}"; Parameters: "--config ""{localappdata}\ChatAuditQQCollector\collector.toml"" diagnose"
Name: "{userstartup}\Chat Audit QQ Collector"; Filename: "{app}\{#AppExeName}"; Parameters: "--config ""{localappdata}\ChatAuditQQCollector\collector.toml"" run"; Tasks: startup

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--config ""{localappdata}\ChatAuditQQCollector\collector.toml"" status"; Description: "显示 Collector 状态"; Flags: postinstall nowait skipifsilent

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
