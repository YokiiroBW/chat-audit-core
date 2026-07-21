# Codex 项目交接说明

更新时间：2026-07-18（Asia/Shanghai）

这份文件用于切换 Codex 账号、迁移开发环境或恢复上下文。新的 Codex 会话
应先完整阅读本文件，再查看代码和运行测试。不要仅根据聊天记录猜测状态。

## 1. 仓库与分支

- 仓库：`YokiiroBW/chat-audit-core`
- 远程：`ssh://git@192.168.31.210:2222/YokiiroBW/chat-audit-core`
- 开发分支：`codex/qqnt-import-q0-q10`
- 基线分支：`main`
- 本文件所在提交：以该分支最新 `HEAD` 为准

关键历史提交：

- `2dd234f feat: add QQNT local database import pipeline`
- `d5c4956 fix: detect QQNT custom VFS databases`

## 2. 本轮目标

为 Chat Audit Core 增加官方 Windows QQ 客户端本地数据库的只读导入能力，
同时保留 NapCat 采集链路，并提供普通用户可操作的 Windows 托盘软件。

原始规格位于本机：

```text
C:\Users\Administrator\Downloads\chat_audit_core_qqnt_local_db_import_dev_spec_v1.md
```

## 3. 已完成内容

### 服务端 Q0-Q5

- QQNT 导入契约、来源、批次、原始记录和媒体状态模型；
- 批量幂等 UPSERT API、权限、审计日志和失败状态；
- `downloaded / not_downloaded / missing / unknown` 媒体源状态；
- `complete / thumbnail_only / metadata_only / failed` 归档状态；
- 前端未下载视频、缩略图、文件丢失和解析失败展示；
- 离线审计与修复规则；
- backup v2、校验和、回滚和兼容测试；
- NapCat 与 QQNT 双来源消息去重及媒体状态升级。

### Collector Q6-Q10

- TOML 配置和敏感字段拒绝；
- Windows DPAPI 凭据存储，Token/数据库密钥不进入 TOML；
- 本地 SQLite 状态库、复合游标、持久上传队列和 dead-letter；
- 媒体 staging、MD5/SHA256、配额和两阶段上传；
- QQNT 数据目录发现、WAL/SHM 文件族和一致性快照；
- 严格只读 SQLite 连接和写操作 authorizer；
- schema probe、基础版本适配和分页读取；
- 文字、表情、图片、语音、视频、文件、回复、卡片、转发和系统消息解析；
- 未知 Protobuf 原始 Base64 留存及可重放解析失败；
- `scan`、定时初始导入、增量重叠回扫、每日对账和重启续传；
- 脱敏诊断包、日志轮转、PyInstaller 和 Inno Setup 配置。

### Windows 托盘软件

新增 `collector/gui/`：

- 首次配置 QQ 账号、设备、QQNT 数据目录、服务器和 API Token；
- Token 通过当前 Windows 用户 DPAPI 加密；
- 状态看板、消息/媒体队列和最近同步结果；
- 数据库格式和兼容性检测；
- 开始、暂停、立即同步和首次导入；
- 模拟消息端到端连接测试；
- dead-letter 重试和一键诊断包；
- 关闭窗口后驻留系统托盘；
- 单实例锁，防止重复启动多个同步器；
- GUI 无控制台单文件 EXE，CLI 继续独立保留。

已实际构建：

```text
dist/chat-audit-qq-collector-gui.exe  约 23.4 MB
dist/chat-audit-qq-collector.exe      约 20.4 MB
```

`dist/` 为忽略目录，迁移后需要重新构建，不能依赖本机现有二进制。

## 4. 真实 QQNT 探测结果与停止规则

本机 QQ 版本：`9.9.31.49738`。

真实主消息库约 410 MB，QQ 运行时 WAL/SHM 活跃。文件头为：

```text
offset 0x00: SQLite header 3\0
offset 0x20: QQ_NT DB
```

官方 `wrapper.node` 内嵌 SQLCipher、`sqlite3_key`、自定义 VFS、扩展头检查和
内部 `pskey` 管理。它不是普通 SQLite，也不是仅提供 SQLCipher 密钥即可
打开的文件。

当前 Collector 会精确返回：

```text
DB_VFS_UNSUPPORTED
```

安全停止规则：

- 不自动提取 `pskey`；
- 不注入 QQ 进程；
- 不修改数据库头；
- 不把未知格式误报成普通密钥错误；
- 不声称真实 QQNT 消息扫描已经成功。

因此尚未执行 20 条真实消息扫描。后续必须先取得合法、可独立调用、与
QQ 9.9.31 兼容的 NT VFS 或官方导出接口，再继续 schema/Protobuf 适配。

详细记录：`docs/real-qqnt-readonly-probe-2026-07-18.md`。

托盘软件在当前真实数据库上会显示“缺少兼容 NT VFS”，并阻止启动真实
数据库同步；“模拟连接测试”仍可用于验证服务器、Token、队列和上传链路。

## 5. 验证状态

最后一次全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp\pytest-gui-final
```

结果：

```text
263 passed in 55.13s
```

额外验证：

- `git diff --check` 通过，仅有 Windows CRLF 提示；
- GUI 和 CLI PyInstaller 构建成功；
- 打包后的 GUI EXE 已实际启动并显示主窗口；
- Tcl/Tk 便携运行时通过构建脚本暂存到 `%TEMP%` 后正确打包；
- GUI 单实例、配置脱敏、真实 QQNT 不兼容提示和托盘图标均有自动化测试。

## 6. 新账号恢复步骤

```powershell
cd C:\Users\Administrator\Documents\chat-audit-core
git fetch
git checkout codex/qqnt-import-q0-q10
git pull --ff-only
```

准备 Python 环境：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r collector\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r collector\packaging\requirements-build.txt
```

重新验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp\pytest-handoff
```

重新构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_collector.ps1
```

启动托盘软件：

```powershell
.\dist\chat-audit-qq-collector-gui.exe
```

## 7. 配置和敏感信息

- 正式 GUI 配置默认位于 `%LOCALAPPDATA%\ChatAuditQQCollector\collector.toml`；
- DPAPI 凭据位于同一目录的 `credentials.dpapi`；
- `.tmp/` 中的探测配置和测试数据库均被 Git 忽略，迁移时不要依赖；
- 不要提交 `.env`、DPAPI 凭据、QQNT 数据库、消息内容或媒体 staging；
- 切换 Windows 用户后 DPAPI 凭据无法解密，需要重新输入 Token。

## 8. 关键代码入口

- 导入契约：`app/import_contract.py`
- 服务端导入：`app/services/import_service.py`
- Collector CLI：`collector/app/main.py`
- Collector 配置：`collector/app/config.py`
- 只读数据库：`collector/qqnt/reader.py`
- QQNT 格式检测：`collector/qqnt/snapshot.py`
- 消息解析：`collector/parsers/message.py`
- 扫描器：`collector/sync/scanner.py`
- 调度器：`collector/sync/scheduler.py`
- 上传器：`collector/sync/uploader.py`
- GUI 控制器：`collector/gui/controller.py`
- GUI 窗口：`collector/gui/app.py`
- 托盘实现：`collector/gui/tray.py`
- CLI 打包：`collector/packaging/chat_audit_qq_collector.spec`
- GUI 打包：`collector/packaging/chat_audit_qq_collector_gui.spec`
- 安装器：`collector/packaging/installer.iss`
- 构建脚本：`scripts/build_collector.ps1`

## 9. 下一步队列

1. 安装 Inno Setup 6，执行 `build_collector.ps1 -Installer` 并验证安装、升级、
   开机启动和卸载保留数据；
2. 使用托盘软件连接实际测试服务器，执行“模拟连接测试”；
3. 在不降低安全边界的前提下调研合法 NT VFS/官方导出接口；
4. 获得兼容 VFS 后才执行 20 条真实消息只读扫描；
5. 再完成百万消息分页和 24 小时稳定性实机验收；
6. 代码评审后合并 `codex/qqnt-import-q0-q10` 到 `main`。

## 10. 明确禁止

- 不要为了“跑通”而修改真实 QQNT 数据库；
- 不要把 Token 或数据库密钥写入 TOML、日志、测试输出或仓库；
- 不要删除本地状态库，除非已确认没有待上传队列；
- 不要绕过 `DB_VFS_UNSUPPORTED` 或伪造真实导入成功；
- 不要恢复或覆盖用户原有的前端轮询和其他无关改动。
