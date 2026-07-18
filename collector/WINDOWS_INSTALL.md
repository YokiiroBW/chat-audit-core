# Windows 安装与运维

## 构建

构建机需要 Python 3.11+。生成安装包还需要 Inno Setup 6，并让
`ISCC.exe` 可从 `PATH` 找到。

```powershell
.\.venv\Scripts\python.exe -m pip install -r collector\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r collector\packaging\requirements-build.txt
.\scripts\build_collector.ps1
.\scripts\build_collector.ps1 -Installer
```

产物位于 `dist`。安装器使用固定 AppId，可直接覆盖升级；用户配置、
游标、日志和待上传队列位于 `%LOCALAPPDATA%\ChatAuditQQCollector`，升级时
不会覆盖现有 `collector.toml`。

## 首次配置

1. 编辑 `%LOCALAPPDATA%\ChatAuditQQCollector\collector.toml`，设置 QQ 号、
   设备标识、QQNT 数据目录和 Chat Audit Core 地址。
2. 在命令行安全写入 API Token；不要把 Token 或数据库密钥写进 TOML。

```powershell
chat-audit-qq-collector.exe --config "$env:LOCALAPPDATA\ChatAuditQQCollector\collector.toml" credential set api_token
chat-audit-qq-collector.exe --config "$env:LOCALAPPDATA\ChatAuditQQCollector\collector.toml" discover
chat-audit-qq-collector.exe --config "$env:LOCALAPPDATA\ChatAuditQQCollector\collector.toml" probe C:\path\to\nt_msg.db --snapshot
chat-audit-qq-collector.exe --config "$env:LOCALAPPDATA\ChatAuditQQCollector\collector.toml" run-once --mode initial
```

安装时可选择登录 Windows 后自动启动。首次历史导入按批次续跑；进程、
Windows 或 NAS 中断后，游标和待上传队列会从本地状态库恢复。

## 故障诊断

```powershell
chat-audit-qq-collector.exe --config "$env:LOCALAPPDATA\ChatAuditQQCollector\collector.toml" status --json
chat-audit-qq-collector.exe --config "$env:LOCALAPPDATA\ChatAuditQQCollector\collector.toml" diagnose
```

诊断包仅包含脱敏运行环境、队列统计、数据库文件元信息和日志尾部。它不
包含 DPAPI 凭据、QQNT 数据库内容、消息正文或暂存媒体。日志按 10 MiB
轮转并保留 5 个历史文件。

## 升级与卸载

- 运行新版安装器即可原位升级。
- 默认保留 `%LOCALAPPDATA%\ChatAuditQQCollector`，以便重装后继续同步。
- 交互式卸载结束时可明确选择删除配置、游标、日志和待上传队列。
- 静默卸载始终保留用户数据，避免无人值守操作误删未上传记录。

当前版本不会猜测未知 QQNT 私有 Protobuf。无法识别的原始记录会以可重放
形式保留，并标记解析失败，需在取得真实版本样本后增加适配器。
