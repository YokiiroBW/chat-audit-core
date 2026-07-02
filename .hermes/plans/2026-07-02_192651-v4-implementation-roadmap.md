# QQ & 微信多租户社交资产审计系统 V4 落地计划

> **For Hermes:** 后续实现时按阶段逐步落地；每个阶段完成后运行验证、提交版本，再推送 Forgejo。

**Goal:** 将现有 V4 架构蓝图落地为可部署、可迭代、可审计的全栈工程。

**Architecture:** 以 FastAPI 作为统一入口，OneBot/NapCat 与未来微信 Hook 通过 adapters 层接入；消息进入 services 层完成全局消息池去重、主视角绑定、媒体内容寻址落盘；数据库通过 SQLAlchemy Async 管理；前端第一阶段以内置静态三栏控制台完成闭环。

**Tech Stack:** FastAPI, SQLAlchemy Async, Alembic, Pydantic Settings, PostgreSQL/SQLite, httpx/aiofiles, Docker Compose, Vue 3 CDN/Element Plus/Tailwind。

---

## 0. 当前上下文

- 已读取蓝图文件：`QQ & 微信多租户社交资产审计系统 —— 全栈工程落地蓝图 (V4 架构).md`
- 当前工作区：`C:\Users\Administrator\Documents\Hermes\QQWXTB`
- 蓝图只有设计文档，尚无工程代码。
- 本机可用工具：Git、Node/NPM、Python 3.11、Docker；`pnpm` 未安装。
- Git 全局用户信息未配置；前期提交可使用仓库级本地身份。
- Forgejo 建仓推送还缺：局域网 Forgejo 基础地址、owner/org、Access Token。

## 1. 蓝图拆解

### 阶段 A：工程基线与仓库准备

1. 建立 README、`.gitignore`、`.env.example`、`data/storage`、`data/backups`。
2. 初始化本地 git 仓库并做第一次提交。
3. 准备 Forgejo 建仓参数，拿到 token 后创建远程仓库并 push `main`。

### 阶段 B：后端基础框架

1. 创建 `app/` Python 包结构。
2. 实现 `app/config.py`：环境变量、数据库 URL、存储路径、OneBot WS 路径。
3. 实现 `app/database.py`：SQLAlchemy Async engine/session 管理。
4. 实现 `app/models.py`：Adapter、Message、RobotMessage、MediaAsset 模型。
5. 引入 Alembic 或首版自动建表命令。
6. 实现健康检查与基础 FastAPI app。

### 阶段 C：消息去重与媒体存储核心

1. 实现 `MessageService.generate_md5()`。
2. 实现媒体内容寻址落盘：MD5 命名、重复文件跳过、数据库索引 upsert。
3. 实现消息入库：`msg_hash` 全局去重。
4. 实现 `RobotMessage` 主视角绑定，确保同一消息可被多个 robot 关联。
5. 增加并发/重复写入测试，避免重复消息造成异常。

### 阶段 D：QQ/NapCat OneBot 11 接入

1. 创建 `app/adapters/onebot11.py`。
2. 提供反向 WebSocket endpoint：`/onebot/v11/ws`。
3. 解析 OneBot 群消息/私聊消息字段，转换为内部统一 message dict。
4. 对 CQ 图片/语音/视频片段预留下载与本地路径重写流程。
5. 用模拟 WebSocket/HTTP 测试消息完成入库验证。

### 阶段 E：查询 API 与游标滚动

1. `GET /api/adapters`：账号列表。
2. `GET /api/rooms?robot_id=...`：指定主视角可见群/会话列表。
3. `GET /api/messages?robot_id=...&room_id=...&before_timestamp=...&limit=50`：游标加载历史消息。
4. `GET /api/search`：第一版全维检索，可先做 room/sender/raw_message 模糊搜索。
5. 保证查询全部走 `RobotMessage` 关联表，不能绕开主视角隔离。

### 阶段 F：内置三栏控制台

1. 放置 `app/static/index.html`。
2. 用 API 替换蓝图里的 mock 数据。
3. 实现机器人账号切换、房间列表加载、消息首次加载、向上滚动加载。
4. 媒体消息根据本地 `local_message` 渲染图片/视频/语音。
5. 提供导出入口按钮但高级导出可放到阶段 G。

### 阶段 G：导出/导入/备份治理

1. 实现条件导出：按平台、robot、room、时间、关键词筛选。
2. 导出包带 manifest、系统识别码、版本号、校验信息。
3. 导入使用 upsert 语义，支持覆盖回录。
4. 自动备份：`data/backups`，先本地定时，后续可接 NAS/群晖路径。

### 阶段 H：Docker 与部署

1. 编写 `requirements.txt`。
2. 编写 Dockerfile，包含 ffmpeg。
3. 编写 `docker-compose.yml`：app + postgres + volume。
4. 编写部署说明：NapCat reverse_ws 指向 `ws://宿主机IP:8000/onebot/v11/ws`。
5. 在本机或局域网机器上跑通 compose。

### 阶段 I：微信扩展预留

1. 创建 `app/adapters/wechat_pc.py` 占位实现。
2. 用同一内部消息模型接收 `platform=wechat` 的模拟消息。
3. 验证数据库和 API 不依赖 QQ 特有字段。

## 2. 第一优先级：我们第一步要做什么

**第一步不是直接写前端，也不是直接接 NapCat；第一步是建立可运行的后端工程基座并锁定数据模型。**

具体第一步验收标准：

1. `app/main.py` 能启动 FastAPI。
2. `app/models.py` 与蓝图 V4 模型一致，并补充唯一约束：`robot_id + msg_hash` 防重复绑定。
3. `app/database.py` 能连接 SQLite 测试库和 PostgreSQL 配置。
4. `MessageService.process_incoming_message()` 有单元测试证明：
   - 同一消息重复进入只产生 1 条 `messages`。
   - 同一消息被两个 robot 看到会产生 2 条 `robot_messages`。
   - 同一 robot 重复看到同一消息不会重复绑定。
5. 通过 `pytest`，再提交 `feat: bootstrap backend foundation`。

## 3. 前期准备清单

- [x] 创建项目 README。
- [x] 创建 `.gitignore`，排除 `.env`、虚拟环境、运行数据、日志、密钥。
- [x] 创建 `.env.example`，记录数据库、存储、OneBot、备份配置。
- [x] 创建 `data/storage/.gitkeep` 与 `data/backups/.gitkeep`。
- [ ] 初始化本地 Git 并提交前期准备版本。
- [ ] 获取 Forgejo 信息并创建远程仓库。

## 4. Forgejo 需要用户提供/确认

为了建仓推送，需要以下信息：

1. Forgejo 基础地址，例如：`http://192.168.1.10:3000`
2. 仓库 owner：个人用户名或组织名；如果不指定，token 用户就是 owner。
3. 仓库名建议：`chat-audit-core` 或 `qq-wx-social-asset-audit`
4. 是否私有：建议 `private=true`
5. Access Token：需要 repo 创建与写入权限。不要发密码。

## 5. 验证策略

- 单元测试：消息哈希、媒体哈希、去重、主视角绑定。
- API 测试：健康检查、账号列表、房间列表、游标消息查询。
- 集成测试：用模拟 OneBot 消息写入数据库，再从 API 读取。
- 部署测试：Docker compose 启动后访问 `/health` 与首页。
- 手工验收：NapCat 群消息、图片/语音/视频落盘和去重。

## 6. 风险与决策

- **数据库选择：**生产建议 PostgreSQL；本地测试可 SQLite，但不能只按 SQLite 特性写代码。
- **MD5：**蓝图使用 MD5；审计系统不是密码存储，内容寻址可接受。若未来强审计可并存 SHA256。
- **并发重复写入：**不能只靠先查后插，最终要靠唯一约束/upsert 兜底。
- **媒体下载：**OneBot CQ 码媒体 URL 可能过期，需要尽快下载落盘并记录失败状态。
- **前端技术：**第一阶段用静态 CDN 简化部署；如内网无法访问 CDN，需要改为本地 vendor 文件或 Vite 构建。
