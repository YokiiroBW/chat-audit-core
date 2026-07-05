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

状态：代码与 compose 支持已完成，NAS 实测两条现有启用路径均未通过，当前已恢复默认安全配置。

已完成：

- 应用侧可选转码开关。
- 语音、视频转码成功路径与失败回退。
- FFmpeg 不可用时自动保存原始文件。
- 默认 Docker 镜像继续保持离线友好，不依赖 apt 源。
- `Dockerfile.ffmpeg` 与 `docker-compose.ffmpeg.yml` 支持联网构建内置 FFmpeg 镜像。
- `docker-compose.ffmpeg-host.yml` 支持宿主机/NAS 已有 FFmpeg 时直接挂载可执行文件。
- `GET /api/system/runtime` 可查看 `ffmpeg_available`、`ffmpeg_version` 与转码配置。

NAS 实测结果：

- 宿主机 `/usr/bin/ffmpeg` 存在，版本 `4.1.9`。
- 使用 `docker-compose.ffmpeg-host.yml` 挂载宿主机二进制后，容器内缺少动态库 `libavdevice.so.58`，runtime 判定 `ffmpeg_available=false`。
- 使用 `docker-compose.ffmpeg.yml` 自动构建内置 FFmpeg 镜像时，NAS 上构建命令长时间卡住，无有效输出；已终止本地等待并恢复普通 compose。
- 恢复后 NAS 服务健康，`/api/system/runtime` 为 `media_transcode_enabled=false`、`ffmpeg_available=false`。

剩余：

- 新增更可靠的启用方案：
  - 方案 A：提供静态 FFmpeg 二进制并通过 volume 挂载。
  - 方案 B：在可联网环境预构建含 FFmpeg 的镜像，推送到 NAS 可拉取的镜像仓库或离线导入。
- 新方案完成后再启用 NAS 转码并验收。

验收：

- `/api/system/runtime` 返回 `ffmpeg_available=true`。
- 语音/视频转码样本可播放。
- 全量测试通过。

### 2. 微信 Hook 专用适配

状态：通用入口已完成，等待最终客户端。

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

- 等最终选定微信 Hook 客户端后，追加客户端专属真实样本。
- 根据真实客户端字段补充专属映射。
- 更新部署说明。

验收：

- 新客户端真实样本可入库并查询。
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
