# 当前工作队列状态

更新时间：2026-07-06

## 已完成并推送

- NAS 外部消息兼容入口：`POST /api/receive_external_msg`。
- NAS multipart 媒体上传入口：`POST /api/external/media`、`POST /api/wechat/media`。
- WeChatFerry 字段归一化：`self_wxid`、`roomid`、`sender`、`msgid`、`type`、`content`、`thumb`、`extra`、`uploaded_path`。
- Windows PC 微信托盘采集器核心骨架：
  - 配置读取
  - NAS 客户端
  - multipart 编码
  - WeChatFerry 消息映射
  - SQLite 离线队列
  - 上传/发送 worker
  - 托盘入口
- PC 端打包与自启脚本：
  - `scripts/build_wechat_tray.ps1`
  - `scripts/install_wechat_tray_startup.ps1`
  - `scripts/uninstall_wechat_tray_startup.ps1`
  - `scripts/write_wechat_tray_config.ps1`
- 文档：`WECHAT_TRAY_ADAPTER.md` 与 README 入口。

## 继续推进队列

1. 托盘端真实 `wcferry` 版本适配和消息读取 API 校准。
2. Windows 桌面托盘 UI 实机验收：图标、菜单、退出、打开 NAS、立即补发。
3. 真实微信端到端样本：私聊、群聊、图片、语音、视频、文件、动画表情、卡片。
4. 托盘端配置编辑入口或最小配置向导。
5. 打包产物版本号和发布包校验。

## 需要用户介入

- 准备已登录官方 PC 微信的 Windows 桌面环境。
- 确认该微信版本可被 WeChatFerry / `wcferry` 支持。
- 提供或现场生成真实消息样本用于验收。
- 允许在真实 Windows 桌面环境安装 `wcferry`、`pystray`、`Pillow`、`PyInstaller` 等 PC 端依赖。

## 当前阻塞说明

真实微信消息读取和托盘 UI 行为无法在当前 NAS/后端测试环境里完成，需要 Windows 桌面和已登录微信。该阻塞不影响后端、核心映射、离线队列和文档继续推进。
