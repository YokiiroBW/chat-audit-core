# 发布前最终代码审计

日期：2026-07-22  
范围：`app/`、`collector/`、前端静态资源、迁移、Docker/Compose、测试与生产依赖  
目标：优先发现灾难性问题，其次识别运行链路、逻辑冲突、重复代码和前端 UI 优化点。

## 结论

**当前不建议直接发布。** 功能测试全部通过，但存在需要先处理的安全、隐私、数据一致性和历史导入性能问题：

- 测试：`280 passed`，完整套件耗时 `506.87s`。
- Python 编译：通过。
- 前端压缩资源一致性：通过。
- `pip check`：通过。
- `pip-audit`：发现 `starlette 0.47.3` 对应 8 条已知漏洞记录，需升级 FastAPI/Starlette 组合后重新回归。
- Docker Compose：当前机器只有 Docker CLI，没有 Compose 插件，未能完成本地 Compose 解析。

## P0：发布阻断项

### 1. 认证媒体目录直接公开

证据：`app/main.py:227-230` 将 `STORAGE_ROOT` 直接挂载到 `public_storage_prefix`，默认路径是 `app/config.py:27` 的 `/static/storage`。

影响：任何能够获得或猜到媒体 URL 的人，不需要 API 鉴权即可访问聊天图片、语音、视频、文件、头像和转发缓存。文件名基于内容哈希，不能视为访问控制；消息 API 返回过 URL 后，URL 可被转发或被日志、浏览器历史泄露。

建议：

1. 将媒体从 `StaticFiles` 公开挂载改为鉴权下载接口。
2. 下载接口校验当前用户/机器人/会话权限，并使用短时签名 URL。
3. 若必须保留静态挂载，至少分离公开头像与私有消息媒体，关闭私有媒体的公开路由。
4. 增加测试：未携带认证访问任意媒体路径必须返回 `401/403`。

### 2. 生产依赖存在已知 Starlette 漏洞

`pip-audit -r requirements-prod.txt` 在 `starlette 0.47.3` 上报告 8 条漏洞记录，涉及 Host/Path URL 重建、Range 解析拒绝服务，以及 Windows `StaticFiles` UNC/SMB 风险。当前依赖由 `fastapi==0.116.1` 间接安装 Starlette，不能只盲目单独升级 Starlette。

建议：

1. 升级到兼容的 FastAPI/Starlette 版本组合，并锁定完整生产依赖。
2. 在 Linux 容器和 Windows Collector/开发环境分别回归静态文件、WebSocket、Range 请求和 OpenAPI。
3. 将 `pip-audit` 纳入 CI 发布门禁。
4. 在反向代理层限制 Host、Forwarded Host 和异常 Range 头。

## P1：高风险项

### 3. 媒体下载存在 SSRF 边界缺口

证据：`app/services/media_service.py:241-249` 接受任意 `http/https` URL；`download_url_to_local_path` 在 `:556-640` 直接调用 `httpx`，没有 DNS/IP 私网、环回、链路本地、云元数据地址或重定向目标校验。

触发面：OneBot 消息中的媒体 URL、转发消息和资料头像缓存都会进入下载链路。

建议：解析并校验每次重定向后的目标；拒绝环回、RFC1918、链路本地、IPv6 本地地址、Unix/UNC 形式和非标准端口；配置允许域名白名单；限制响应体读取为流式分块；记录最终目标和拒绝原因。

### 4. 前端对所有 HTTP 失败和网络失败重试 POST/PATCH/DELETE

证据：`app/static/assets/app.js:167-211` 的 `requestWithRetry` 对所有方法统一重试；调用方包含备份、导入、管理员 Token/用户创建、密码重置、离线修复和适配器变更。

影响：服务端已经提交但响应丢失时，客户端重试会重复执行副作用操作，可能产生重复备份、重复 Token、错误的“创建失败”提示，或触发二次修复。

建议：默认只重试 `GET/HEAD/OPTIONS`；写操作必须使用显式幂等键，服务端按幂等键缓存结果；对备份、导入、Token 创建等高风险操作禁止自动重试。

### 5. 备份恢复不是文件系统与数据库原子事务

证据：`app/services/backup_service.py:980-1000` 先写入嵌入媒体，`app/services/backup_service.py:1482-1483` 随后才提交数据库。

影响：数据库提交失败、进程崩溃或空间不足时，媒体文件可能已经覆盖现有文件，但数据库