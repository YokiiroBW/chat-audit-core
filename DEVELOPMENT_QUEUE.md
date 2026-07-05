# 开发队列

本队列承接当前审计出的未完成项目。后续每完成一个可验收版本，按以下流程推进：

1. 更新代码与测试。
2. 运行相关测试和全量测试。
3. 使用中文提交信息提交。
4. 推送到局域网 Forgejo 仓库。
5. 对需要部署的功能，部署到 NAS 并执行接入验收。

## P0 当前无阻塞项

当前主线可用，全量测试通过。下面项目为后续能力增强和生产化收口。

## P1 优先开发

### 1. FFmpeg 与媒体转码流水线

状态：已完成并在 NAS 验收通过。

已完成：

- 应用侧可选转码开关。
- 语音、视频转码成功路径与失败回退。
- FFmpeg 不可用时自动保存原始文件。
- 默认 Docker 镜像继续保持离线友好，不依赖 apt 源。
- `Dockerfile.ffmpeg` 与 `docker-compose.ffmpeg.yml` 支持离线安装 `imageio-ffmpeg` wheel，构建内置静态 FFmpeg 镜像。
- `docker-compose.ffmpeg-host.yml` 支持宿主机/NAS 已有 FFmpeg 时直接挂载可执行文件。
- `GET /api/system/runtime` 可查看 `ffmpeg_available`、`ffmpeg_version` 与转码配置。

NAS 实测结果：

- 宿主机 `/usr/bin/ffmpeg` 存在，版本 `4.1.9`。
- 使用 `docker-compose.ffmpeg-host.yml` 挂载宿主机二进制后，容器内缺少动态库 `libavdevice.so.58`，runtime 判定 `ffmpeg_available=false`。
- 宿主机动态库挂载后可以完成版本探测，但实际 WAV 转 MP3 smoke test 出现段错误，不作为推荐启用路径。
- `docker-compose.ffmpeg.yml` 使用 vendored `imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl` 后，NAS 构建成功。
- NAS 容器内 `ffmpeg version 7.0.2-static` 可用，`/api/system/runtime` 返回 `ffmpeg_available=true`。
- WAV 转 MP3 smoke test 通过。

验收：

- 继续保留回归测试，保证 `Dockerfile.ffmpeg` 不回退到 apt 构建。
- 后续真实语音/视频样本接入时，补充端到端可播放验收。

### 2. 微信 PC 托盘采集适配器

状态：通用入口已完成；下一阶段实现 Windows PC 静默托盘采集器，托盘软件内置集成 WeChatFerry。

已完成：

- `POST /api/wechat/events` 通用 Hook 接收入口。
- 支持常见微信 Hook 字段名自动归一化。
- 文本、图片、语音、视频、文件、表情、分享卡片可转为内部消息/CQ 表达。
- 入库后使用 `platform=wechat`，复用媒体缓存、查询和资料缓存。
- 支持顶层、`data`、`payload`、`msg`、`message` 嵌套字段。
- 支持常见字段大小写差异和数字 `MsgType`。
- 支持群聊文本中的 `sender_wxid:\n内容` 前缀拆分。
- `tests/fixtures/wechat_hook_samples.json` 样本回放覆盖文本、图片、语音、表情、分享卡片和群聊发送者前缀。

剩余：

- NAS 后端增加外部消息接收兼容入口，例如 `POST /api/receive_external_msg`，并继续兼容 `POST /api/wechat/events`。
- NAS 后端增加 Multipart 文件上传接口，供 PC 端上传图片、语音、视频和文件。
- 新增 WeChatFerry 专属字段归一化层，覆盖 `wxid`、`roomid`、`sender`、`msg_id`、`type`、`content`、`thumb`、`extra` 和本地文件路径。
- 新建 Windows PC 端 Python 托盘程序，默认无控制台、无主窗口，仅系统托盘图标。
- 托盘程序直接依赖并调用 `wcferry`/WeChatFerry，不要求用户单独启动 WeChatFerry 服务。
- 托盘程序支持 NAS 地址、Token、本机账号、开机自启、媒体自动下载和日志路径配置。
- 托盘程序支持断线队列、自动重连、失败重试、上传状态和本地日志。
- 托盘菜单提供连接状态、同步状态、打开 NAS 控制台、暂停/继续同步和退出。
- 增加 WeChatFerry 真实样本回放、PC 端安装/打包说明和常见错误排查。

验收：

- 托盘程序可通过 `pythonw.exe` 或 PyInstaller `--noconsole` 静默运行。
- 官方 PC 微信收到的新消息可自动进入 NAS 并在 Web UI 查询。
- 图片、语音、视频、文件可自动下载、上传、缓存并离线打开。
- NAS 不可达时本地队列保留，恢复后自动补发，重复 `message_id` 不重复入库。
- `platform=wechat` 不被 QQ 专属逻辑覆盖。
- 全量测试通过。

## P2 已完成生产化增强

- 导出包系统识别码签名：已完成。
- 自动备份前端配置入口：已完成。
- 生产权限、限流与操作审计：已完成。
- 数据库迁移体系：已完成。
- Forgejo SSH 鉴权：已完成。
- 角色抓取黑白名单与内容项策略：已完成。

## P3 运维和文档收口

### 3. 交接文档持续更新

状态：持续项。

目标：

- 每次版本推进后更新 `PROJECT_HANDOFF_READ_ME_FIRST.md`、`TASK_QUEUE.md` 和本文件。
- 记录最新提交、测试数量、NAS 状态和剩余队列。
- 不写入 token、密码或私钥。

验收：

- 文档与当前主线一致。
- 后续接手无需翻完整聊天记录。
