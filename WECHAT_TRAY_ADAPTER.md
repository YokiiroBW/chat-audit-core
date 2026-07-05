# 微信 PC 托盘采集适配器

本目录内的 `wechat_tray_adapter` 是 Windows PC 端采集器骨架，目标是启动后无命令提示行、无默认主窗口，只在系统托盘显示图标，并在内部集成 `wcferry` / WeChatFerry 读取官方 PC 微信消息。

## 当前已完成

- NAS 端新增 `POST /api/receive_external_msg` 外部消息兼容入口，复用现有微信入库逻辑。
- NAS 端新增 `POST /api/external/media` 与 `POST /api/wechat/media` 标准 multipart 媒体上传入口。
- WeChatFerry 常见字段已归一化：`self_wxid`、`roomid`、`sender`、`msgid`、`type`、`content`、`thumb`、`extra`、`uploaded_path`。
- PC 端核心包已具备配置读取、NAS 客户端、消息映射、SQLite 离线队列、上传/发送 worker。
- 托盘入口已预留，运行时按需加载 `pystray`、`Pillow` 和 `wcferry`。

## 配置文件

默认配置路径：

```text
%APPDATA%\ChatAuditWechatTray\config.json
```

示例：

```json
{
  "nas_url": "http://192.168.31.210:8000",
  "token": "replace-with-operator-token",
  "account_id": "wxid_xxx",
  "account_name": "微信采集账号",
  "auto_download_media": true,
  "autostart": false,
  "paused": false
}
```

也支持环境变量覆盖：

```text
CHAT_AUDIT_NAS_URL
CHAT_AUDIT_TOKEN
CHAT_AUDIT_WECHAT_ACCOUNT_ID
CHAT_AUDIT_WECHAT_ACCOUNT_NAME
```

## 静默启动

开发期可以用 `pythonw.exe` 启动，避免出现命令提示行：

```powershell
.\.venv\Scripts\pythonw.exe -m wechat_tray_adapter
```

打包为 exe 时应使用无控制台参数：

```powershell
python -m PyInstaller --noconsole --name chat-audit-wechat-tray wechat_tray_adapter\__main__.py
```

仓库提供了便捷脚本：

```powershell
.\scripts\build_wechat_tray.ps1
```

该脚本会安装 `wechat_tray_adapter/requirements.txt` 中的 PC 端可选依赖，并用 PyInstaller 生成无控制台 exe。
构建完成后会在 `dist\chat-audit-wechat-tray\manifest.json` 写入版本号、exe 文件名、SHA256 和构建时间。

## 配置与自启脚本

生成配置：

```powershell
.\scripts\write_wechat_tray_config.ps1 `
  -NasUrl "http://192.168.31.210:8000" `
  -Token "replace-with-operator-token" `
  -AccountId "wxid_xxx" `
  -AccountName "微信采集账号"
```

安装当前用户开机自启：

```powershell
.\scripts\install_wechat_tray_startup.ps1 -ExePath ".\dist\chat-audit-wechat-tray\chat-audit-wechat-tray.exe"
```

卸载当前用户开机自启：

```powershell
.\scripts\uninstall_wechat_tray_startup.ps1
```

## 仍需真实环境验收

以下项目必须在你的 Windows 微信环境里完成：

- 官方 PC 微信已登录，且版本与 WeChatFerry 兼容。
- 安装并验证 `wcferry` 可在本机读取消息。
- 用真实私聊、群聊、图片、语音、视频、文件、表情、卡片样本跑端到端同步。
- 确认托盘图标、退出、打开 NAS、立即补发队列在 Windows 桌面环境表现正常。

这些项目不阻塞 NAS 后端和核心同步逻辑的继续开发；真实环境不可用时，记录为外部阻塞并继续推进其他队列。
