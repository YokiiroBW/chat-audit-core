# QQ & 微信多租户社交资产审计系统 - 代码审计报告

**审计时间**: 2026-07-06  
**审计范围**: 完整代码仓库（只读审计，不修改代码）  
**代码规模**: 约 200 个 Python 文件，前端 2678 行 JS + 1807 行 CSS  
**最新提交**: `5a773ca UI：修复图片气泡宽度并识别蓝链`  
**测试覆盖**: 165 个测试用例通过

---

## 一、发现项汇总（按优先级排序）

### P0 级别（安全/数据完整性/生产阻塞）- 0 项

✅ **无 P0 级别问题**

### P1 级别（BUG/重要安全加固/重要性能问题）- 8 项

### P2 级别（代码质量/可维护性/次要性能）- 15 项

### P3 级别（优化建议/文档完善/测试增强）- 12 项

**总计**: 35 项发现

---

## 二、详细发现项

### P1-01: WebSocket 连接未做并发保护

**类型**: BUG/并发安全  
**优先级**: P1

**问题描述**:  
`app/ws.py` 中的 WebSocket 处理器和 `OneBotRPCService` 在同一 `robot_id` 重复连接时存在竞态条件：

1. `OneBotRPCService._connections` 是全局字典，未加锁
2. 同一 `robot_id` 多次连接时，后来的连接会覆盖前一个，但前一个连接的后台任务仍在运行
3. `unregister_connection` 按 WebSocket 对象反查 `robot_id`，可能清理错误的连接

**影响范围**:

- `app/ws.py:143-214`
- `app/services/onebot_rpc_service.py:8-55`

**建议修改方案**:

```python
# onebot_rpc_service.py
import asyncio
from typing import Any

class OneBotRPCService:
    _connections: dict[str, tuple[WebSocket, str]] = {}  # robot_id -> (websocket, connection_id)
    _lock: asyncio.Lock = asyncio.Lock()
    _pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @classmethod
    async def register_connection(cls, robot_id: str, websocket: WebSocket) -> str:
        """Returns connection_id for cleanup."""
        connection_id = uuid.uuid4().hex
        async with cls._lock:
            old = cls._connections.get(robot_id)
            if old is not None:
                # 通知旧连接断开
                pass  
            cls._connections[robot_id] = (websocket, connection_id)
        return connection_id

    @classmethod
    async def unregister_connection(cls, robot_id: str, connection_id: str) -> None:
        async with cls._lock:
            current = cls._connections.get(robot_id)
            if current and current[1] == connection_id:
                cls._connections.pop(robot_id, None)
```

**验收标准**:

- 同一 `robot_id` 多次快速连接断开，不会出现 RPC 调用发送到已关闭的连接
- 旧连接的后台任务不会干扰新连接

**推荐测试命令**:

```python
pytest tests/test_onebot_rpc_service.py -k concurrent -v
```

**适合便宜模型执行**: 否（涉及并发逻辑，需要仔细设计）

---

### P1-02: 媒体下载缺少文件类型白名单验证

**类型**: 安全/DoS  
**优先级**: P1

**问题描述**:  
`app/services/media_service.py` 的 `_download_media` 函数：

1. 只检查 `Content-Length`，未验证 `Content-Type`
2. 恶意 NapCat 可返回超大 HTML/ZIP 文件伪装成图片
3. `MEDIA_MAX_BYTES=104857600` (100MB) 对单个文件来说过大，可能被滥用

**影响范围**:

- `app/services/media_service.py:350-430`
- 存储空间耗尽风险

**建议修改方案**:

```python
ALLOWED_CONTENT_TYPES = {
    "image": {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"},
    "voice": {"audio/mpeg", "audio/wav", "audio/ogg", "audio/silk", "audio/amr", "audio/mp4", "application/octet-stream"},
    "video": {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"},
    "file": {"application/octet-stream", "application/zip", "application/pdf"},
}

async def _download_media(...):
    # ... 现有代码 ...
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    allowed_types = ALLOWED_CONTENT_TYPES.get(media_type, set())
    if content_type and allowed_types and content_type not in allowed_types:
        raise ValueError(f"Content-Type {content_type} not allowed for {media_type}")
```

同时建议降低单文件限制：

- 图片/语音: 10MB
- 视频: 50MB
- 文件: 20MB（或完全禁用 `capture_file` 默认值已是 False）

**验收标准**:

- 尝试下载伪装 Content-Type 的恶意文件，应被拒绝
- 合法媒体文件正常下载

**推荐测试命令**:

```python
pytest tests/test_media_service.py::test_download_media_content_type_validation -v
```

**适合便宜模型执行**: 是

---

### P1-03: 数据库连接池未配置，高并发可能耗尽连接

**类型**: 性能/部署风险  
**优先级**: P1

**问题描述**:  
`app/database.py:53-60` 创建引擎时未设置连接池参数：

```python
engine = create_async_engine(url, future=True)
```

SQLAlchemy 默认连接池大小为 5，对于生产环境不足：

1. 每个 WebSocket 长连接占用一个会话
2. HTTP API 并发请求未做连接复用
3. PostgreSQL 默认最大连接数 100，容易耗尽

**影响范围**:

- `app/database.py:53-60`
- 所有数据库操作

**建议修改方案**:

```python
from sqlalchemy.pool import NullPool

def create_async_engine_and_sessionmaker(database_url: str | None = None) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = database_url or get_settings().database_url
    
    # SQLite 使用 NullPool（不支持并发写）
    if url.startswith("sqlite"):
        engine = create_async_engine(
            url, 
            future=True,
            poolclass=NullPool,
            connect_args={"check_same_thread": False}
        )
    else:
        # PostgreSQL 生产环境
        engine = create_async_engine(
            url,
            future=True,
            pool_size=20,          # 正常连接数
            max_overflow=10,       # 峰值额外连接
            pool_timeout=30,       # 获取连接超时
            pool_recycle=3600,     # 1小时回收连接（防止 PostgreSQL idle timeout）
            pool_pre_ping=True,    # 使用前检查连接有效性
        )
    
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, sessionmaker
```

**验收标准**:

- 100 并发 HTTP 请求不会出现连接池耗尽错误
- SQLite 开发环境仍可正常运行

**推荐测试命令**:

```bash
# 使用 locust 或 ab 进行并发测试
ab -n 1000 -c 50 http://localhost:8000/health
```

**适合便宜模型执行**: 是

---

### P1-04: 后台任务异常未捕获，可能导致静默失败

**类型**: BUG/可观测性  
**优先级**: P1

**问题描述**:  
`app/ws.py:196-205` 的后台任务使用 `asyncio.create_task` 但未处理异常：

```python
track_background_task(_hydrate_forward_payloads(normalized.robot_id, msg_hash))
track_background_task(_hydrate_user_profile(...))
track_background_task(_hydrate_group_profile(...))
```

如果这些任务抛异常（网络超时、数据库锁等），会：

1. 静默失败，不记录日志
2. 用户看不到头像/群名/合并转发详情，但不知道原因
3. `finally` 块的 `task.cancel()` 无法清理已完成但失败的任务

**影响范围**:

- `app/ws.py:59-141` (后台任务函数)
- `app/ws.py:196-212` (任务调度)

**建议修改方案**:

```python
import logging

logger = logging.getLogger(__name__)

def track_background_task(coro):
    task = asyncio.create_task(_safe_background_task(coro))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

async def _safe_background_task(coro):
    try:
        await coro
    except asyncio.CancelledError:
        raise  # 正常取消，不记录
    except Exception as exc:
        logger.exception(f"Background task failed: {exc}")
        # 可选：写入 audit_logs 表
```

**验收标准**:

- 后台任务失败时在日志中可见
- 不影响主消息入库流程

**推荐测试命令**:

```python
pytest tests/test_onebot11.py::test_background_task_error_handling -v
```

**适合便宜模型执行**: 是

---

### P1-05: 密码哈希使用了不安全的 SHA256

**类型**: 安全  
**优先级**: P1

**问题描述**:  
`app/services/admin_user_service.py:22-24` 使用简单的 SHA256 哈希密码：

```python
@staticmethod
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()
```

SHA256 不是密码哈希算法，存在以下问题：

1. 无盐值（salt），相同密码哈希相同，容易彩虹表攻击
2. 计算速度快，易被暴力破解
3. 不符合 OWASP 密码存储最佳实践

**影响范围**:

- `app/services/admin_user_service.py:22-27`
- `admin_users` 表中所有密码

**建议修改方案**:

```python
import bcrypt

class AdminUserService:
    @staticmethod
    def hash_password(password: str) -> str:
        salt = bcrypt.gensalt(rounds=12)  # 成本因子 12
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
    
    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
```

或使用 `passlib`:

```python
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class AdminUserService:
    @staticmethod
    def hash_password(password: str) -> str:
        return pwd_context.hash(password)
    
    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        return pwd_context.verify(password, password_hash)
```

需要添加依赖：

```
bcrypt==4.1.2
# 或
passlib[bcrypt]==1.7.4
```

**数据迁移**:  
需要创建迁移脚本强制所有用户重置密码，或在首次登录时重新哈希。

**验收标准**:

- 新密码使用 bcrypt 存储
- 相同密码的哈希值不同（有盐值）
- 旧密码仍可登录（兼容期）

**推荐测试命令**:

```python
pytest tests/test_admin_user_auth.py::test_password_hashing_secure -v
```

**适合便宜模型执行**: 是（但需要明确数据迁移策略）

---

### P1-06: SQL 注入风险 - 动态表名未验证

**类型**: 安全  
**优先级**: P1

**问题描述**:  
`app/database.py:78-79` 的 `_table_columns` 函数直接使用 `table_name` 参数：

```python
async def _table_columns(conn, table_name: str) -> set[str]:
    return await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns(table_name)})
```

虽然当前所有调用都是硬编码的表名，但如果未来有新代码传入用户输入，会有 SQL 注入风险。

同时 `_record_migration` 函数的 `text()` 使用了参数化查询，是安全的。

**影响范围**:

- `app/database.py:78-79`

**建议修改方案**:

```python
VALID_TABLE_NAMES = {
    "adapters", "bot_profiles", "capture_target_policies", "room_profiles",
    "user_profiles", "messages", "robot_messages", "media_assets",
    "audit_logs", "admin_tokens", "admin_users", "admin_sessions",
    "system_settings", "schema_migrations"
}

async def _table_columns(conn, table_name: str) -> set[str]:
    if table_name not in VALID_TABLE_NAMES:
        raise ValueError(f"Invalid table name: {table_name}")
    return await conn.run_sync(lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns(table_name)})
```

**验收标准**:

- 传入非法表名抛出异常
- 现有迁移正常运行

**推荐测试命令**:

```python
pytest tests/test_database.py::test_table_columns_validation -v
```

**适合便宜模型执行**: 是

---

### P1-07: 媒体回填可能造成数据库死锁

**类型**: BUG/数据完整性  
**优先级**: P1

**问题描述**:  
`app/services/media_backfill_service.py:86-180` 在循环中对每条消息执行：

```python
for message in messages:
    # ... 下载媒体 ...
    message.local_message = updated
    await session.commit()  # 每条消息都提交
```

问题：

1. 频繁提交增加事务开销
2. 与 WebSocket 入库并发时可能死锁（都在更新 `messages` 表）
3. 大批量回填时长时间持有会话

**影响范围**:

- `app/services/media_backfill_service.py:86-180`
- `app/api.py:1365-1380` (回填 API)

**建议修改方案**:

```python
# 批量提交
BATCH_SIZE = 50
updated_count = 0

for i, message in enumerate(messages):
    # ... 处理逻辑 ...
    if updated:
        message.local_message = updated
        updated_count += 1
    
    # 每 50 条提交一次
    if (i + 1) % BATCH_SIZE == 0:
        await session.commit()

# 最后提交剩余
if updated_count % BATCH_SIZE != 0:
    await session.commit()
```

同时建议加分布式锁或限制同时只能运行一个回填任务。

**验收标准**:

- 回填 1000 条消息不会超时或死锁
- 回填期间新消息仍可正常入库

**推荐测试命令**:

```python
pytest tests/test_media_backfill_service.py::test_concurrent_backfill -v
```

**适合便宜模型执行**: 是

---

### P1-08: 前端未做 CSRF 防护

**类型**: 安全  
**优先级**: P1

**问题描述**:  
管理 API（导出、导入、删除、强制下线等）使用 Bearer Token 鉴权，但未做 CSRF 防护：

1. Token 存储在 `localStorage`
2. 前端每次请求自动带 `Authorization` header
3. 恶意网站可诱导用户访问，触发敏感操作

**影响范围**:

- `app/static/assets/app.js` (所有 API 调用)
- `app/api.py` (所有需要 Token 的端点)

**建议修改方案**:

**方案一：添加 CSRF Token**

```python
# app/api.py
from starlette.middleware.csrf import CSRFMiddleware

app.add_middleware(
    CSRFMiddleware,
    secret=settings.app_secret_key,
)

# 前端需要从 cookie 读取 CSRF token 并附加到 header
```

**方案二：使用 SameSite Cookie**

```python
# 改用 HttpOnly SameSite Cookie 存储 session token
# 前端不再用 localStorage

from fastapi.responses import Response

@router.post("/auth/login")
async def login(...):
    # ...
    response = Response(content=...)
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        secure=True,  # 生产环境 HTTPS
        samesite="strict",
        max_age=86400 * 7,
    )
    return response
```

**方案三：双重提交 Cookie**（最简单）

```python
# API 验证时检查 cookie 中的 token 与 header 中的一致
```

**验收标准**:

- 跨站请求无法触发敏感操作
- 正常用户操作不受影响

**推荐测试命令**:  
手动测试：在恶意页面发起跨站请求，应被拒绝

**适合便宜模型执行**: 否（需要权衡方案和前后端配合）

---

### P2-01: 环境变量验证不完整

**类型**: 部署风险  
**优先级**: P2

**问题描述**:  
`app/main.py:38-49` 的 `validate_production_settings` 只检查了几个关键变量：

- 遗漏 `DATABASE_URL` 验证（仍用 SQLite 会有性能问题）
- 遗漏 `SYSTEM_INSTANCE_ID` 验证（影响备份签名）
- 不检查 `STORAGE_ROOT` 和 `BACKUP_ROOT` 权限

**影响范围**:

- `app/main.py:38-49`

**建议修改方案**:

```python
def validate_production_settings(settings: Settings) -> None:
    if settings.app_env.lower() != "production":
        return
    
    # 现有检查...
    
    # 数据库检查
    if "sqlite" in settings.database_url.lower():
        raise ValueError("SQLite is not recommended for production; use PostgreSQL")
    
    # 实例 ID 检查
    if settings.system_instance_id in {"", "chat-audit-core"}:
        raise ValueError("SYSTEM_INSTANCE_ID must be unique in production for backup signatures")
    
    # 存储目录检查
    for path_name, path in [("STORAGE_ROOT", settings.storage_root), ("BACKUP_ROOT", settings.backup_root)]:
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise ValueError(f"{path_name} {path} cannot be created: {exc}") from exc
        if not os.access(path, os.W_OK):
            raise ValueError(f"{path_name} {path} is not writable")
```

**验收标准**:

- 生产环境用 SQLite 启动失败并报错
- 存储目录不可写时启动失败

**推荐测试命令**:

```python
pytest tests/test_app_factory.py::test_production_validation -v
```

**适合便宜模型执行**: 是

---

### P2-02: 时区处理不一致

**类型**: BUG/数据完整性  
**优先级**: P2

**问题描述**:  
项目中时间戳混用了三种表示：

1. Unix 秒级时间戳 (`messages.timestamp`): 来自 OneBot，无时区信息
2. UTC datetime (`created_at`, `updated_at`): 数据库字段
3. 本地时间：前端 `formatTs` 用 `new Date(ts * 1000).toLocaleString()`，依赖用户浏览器时区

问题：

- OneBot 的 `time` 字段未明确是本地时间还是 UTC
- 备份导出时 `created_at` 序列化为 ISO 字符串，导入时未指定时区
- 跨时区迁移可能导致时间偏移

**影响范围**:

- `app/time_utils.py` (只有 `utc_now()`)
- `app/models.py` (所有 DateTime 字段)
- `app/services/backup_service.py` (导入导出)

**建议修改方案**:

```python
# app/time_utils.py
from datetime import datetime, timezone

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def timestamp_to_utc(ts: int) -> datetime:
    """将 Unix 秒级时间戳转为 UTC datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)

def utc_to_timestamp(dt: datetime) -> int:
    """将 datetime 转为 Unix 秒级时间戳."""
    return int(dt.timestamp())
```

数据库模型明确使用 timezone-aware:

```python
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

class TZDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return value

# models.py
created_at = Column(TZDateTime, default=utc_now, nullable=False)
```

**验收标准**:

- 所有时间存储为 UTC
- 导出导入保持时间一致
- 前端显示用户本地时间

**推荐测试命令**:

```python
pytest tests/test_time_utils.py -v
```

**适合便宜模型执行**: 是

---

### P2-03: 缺少 API 请求大小限制

**类型**: 安全/DoS  
**优先级**: P2

**问题描述**:  
导入 API (`POST /api/import`) 接受整个 JSON 包体，没有大小限制：

- 恶意用户可发送 GB 级 JSON 导致内存耗尽
- FastAPI 默认无请求体大小限制

**影响范围**:

- `app/api.py:1408-1431` (导入 API)
- `app/api.py:1392-1405` (验证 API)

**建议修改方案**:

```python
# app/main.py
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.status import HTTP_413_REQUEST_ENTITY_TOO_LARGE

MAX_REQUEST_SIZE = 100 * 1024 * 1024  # 100MB

@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_REQUEST_SIZE:
        return Response(
            content=json.dumps({"detail": "Request body too large"}),
            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            media_type="application/json",
        )
    return await call_next(request)
```

或使用 `python-multipart` 的限制功能。

**验收标准**:

- 超过 100MB 的请求被拒绝
- 正常请求不受影响

**推荐测试命令**:

```python
pytest tests/test_api_limits.py::test_request_size_limit -v
```

**适合便宜模型执行**: 是

---

### P2-04: 备份文件名可预测，存在时序攻击风险

**类型**: 安全  
**优先级**: P2

**问题描述**:  
`app/services/backup_service.py` 的备份文件名格式：

```python
f"backup_{timestamp}_{system_id}.json"
```

问题：

- 文件名完全可预测（时间戳 + 系统 ID）
- 如果备份目录通过 HTTP 暴露，攻击者可遍历下载
- 建议添加随机后缀

**影响范围**:

- `app/services/backup_service.py` (文件名生成)
- `data/backups/` 目录

**建议修改方案**:

```python
import secrets

def _generate_backup_filename(system_id: str) -> str:
    timestamp = int(time.time())
    random_suffix = secrets.token_hex(8)  # 16 字符随机后缀
    return f"backup_{timestamp}_{system_id}_{random_suffix}.json"
```

**验收标准**:

- 备份文件名不可预测
- 旧备份文件名兼容（清理逻辑仍可识别）

**推荐测试命令**:

```python
pytest tests/test_backup_service.py::test_backup_filename_unpredictable -v
```

**适合便宜模型执行**: 是

---

### P2-05: 前端缺少输入验证和 XSS 防护

**类型**: 安全  
**优先级**: P2

**问题描述**:  
`app/static/assets/app.js` 中多处直接插入用户输入：

```javascript
item.innerHTML = `<strong>${message.nickname}</strong>: ${message.local_message}`;
```

虽然 `local_message` 来自后端，但如果：

1. 恶意 OneBot 发送包含 `<script>` 的消息
2. 后端未转义直接存储
3. 前端直接用 `innerHTML` 渲染

会导致存储型 XSS。

**影响范围**:

- `app/static/assets/app.js` (所有 `innerHTML` 使用)
- 消息渲染、昵称显示、群名显示

**建议修改方案**:

```javascript
// 添加 HTML 转义函数
const escapeHtml = (text) => {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
};

// 或使用 DOMPurify
const sanitizeHtml = (html) => DOMPurify.sanitize(html, {
  ALLOWED_TAGS: ['a', 'img', 'br', 'strong', 'em'],
  ALLOWED_ATTR: ['href', 'src', 'alt', 'class'],
});

// 使用 textContent 而非 innerHTML
item.textContent = message.nickname;

// 或使用模板引擎
```

同时后端也应验证：

```python
# app/adapters/onebot11.py
import html

def normalize_message_event(event: dict[str, Any]) -> NormalizedMessageEvent | None:
    # ...
    raw_message = html.escape(str(raw_message))  # 转义特殊字符
```

**验收标准**:

- 包含 `<script>alert('xss')</script>` 的消息不会执行
- 正常消息显示不受影响

**推荐测试命令**:  
手动测试 + 浏览器开发者工具检查

**适合便宜模型执行**: 是（前端部分）

---

### P2-06: 日志记录不足，难以排查生产问题

**类型**: 可观测性  
**优先级**: P2

**问题描述**:  
项目缺少结构化日志：

1. 只有审计日志（`audit_logs` 表），没有应用日志
2. WebSocket 消息处理、媒体下载、后台任务失败等关键路径无日志
3. 生产环境排查问题困难

**影响范围**:

- 整个 `app/` 目录

**建议修改方案**:

```python
# app/logging_config.py
import logging
import sys
from pythonjsonlogger import jsonlogger

def setup_logging(log_level: str = "INFO"):
    logger = logging.getLogger()
    logger.setLevel(log_level)
    
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# app/main.py
from app.logging_config import setup_logging

def create_app(...):
    setup_logging(active_settings.log_level)
    # ...
```

在关键位置添加日志：

```python
# app/ws.py
logger = logging.getLogger(__name__)

async def onebot11_reverse_ws(...):
    logger.info("OneBot WebSocket connected", extra={
        "adapter_id": adapter_id,
        "robot_id": robot_id,
    })
    
    try:
        # ...
    except Exception as exc:
        logger.exception("WebSocket handler error", extra={
            "adapter_id": adapter_id,
            "error": str(exc),
        })
```

添加依赖：

```
python-json-logger==2.0.7
```

**验收标准**:

- 生产环境日志可导出到 ELK/Loki
- 关键操作有 trace

**推荐测试命令**:  
手动检查日志输出

**适合便宜模型执行**: 是

---

### P2-07: FFmpeg 转码未设置超时，可能被恶意文件卡住

**类型**: DoS  
**优先级**: P2

**问题描述**:  
`app/services/media_service.py` 的 FFmpeg 转码调用：

```python
process = await asyncio.create_subprocess_exec(...)
stdout, stderr = await process.communicate()
```

没有设置超时，恶意构造的媒体文件可能导致 FFmpeg 无限等待。

**影响范围**:

- `app/services/media_service.py:268-330` (转码函数)

**建议修改方案**:

```python
async def _transcode_media(...):
    # ...
    process = await asyncio.create_subprocess_exec(...)
    
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=60.0  # 60 秒超时
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError(f"FFmpeg transcode timeout after 60s")
```

**验收标准**:

- 超时自动终止 FFmpeg 进程
- 正常转码不受影响

**推荐测试命令**:

```python
pytest tests/test_media_service.py::test_ffmpeg_timeout -v
```

**适合便宜模型执行**: 是

---

### P2-08: 合并转发递归深度未限制

**类型**: DoS  
**优先级**: P2

**问题描述**:  
`app/services/media_service.py:915-1000` 的 `cache_cq_forward_payloads` 会递归处理嵌套的合并转发，但没有深度限制。

恶意消息可构造深层嵌套导致：

1. 栈溢出
2. 数据库递归查询超时
3. 内存耗尽

**影响范围**:

- `app/services/media_service.py:915-1000`

**建议修改方案**:

```python
MAX_FORWARD_DEPTH = 5

async def cache_cq_forward_payloads(
    session: AsyncSession,
    local_message: str,
    forward_loader: Any,
    http_client: Any,
    storage_root: str | Path | None = None,
    public_prefix: str | None = None,
    max_bytes: int | None = None,
    allowed_media_types: set[str] | None = None,
    _depth: int = 0,  # 新增深度参数
) -> str:
    if _depth >= MAX_FORWARD_DEPTH:
        return local_message  # 超过深度限制，不继续展开
    
    # ... 现有逻辑 ...
    
    # 递归调用时传递深度
    child_message = await cache_cq_forward_payloads(
        session,
        raw_child_message,
        forward_loader,
        http_client,
        storage_root,
        public_prefix,
        max_bytes,
        allowed_media_types,
        _depth=_depth + 1,  # 深度 +1
    )
```

**验收标准**:

- 超过 5 层嵌套的合并转发不会继续展开
- 正常合并转发功能不受影响

**推荐测试命令**:

```python
pytest tests/test_media_service.py::test_forward_depth_limit -v
```

**适合便宜模型执行**: 是

---

### P2-09: 数据库迁移缺少回滚机制

**类型**: 部署风险  
**优先级**: P2

**问题描述**:  
`app/database.py` 的轻量迁移只有 `apply` 没有 `rollback`：

```python
@dataclass(frozen=True)
class LightweightMigration:
    version: str
    description: str
    apply: MigrationApply
    # 缺少 rollback
```

虽然 Alembic 支持回滚，但轻量迁移不支持，生产环境一旦迁移失败很难恢复。

**影响范围**:

- `app/database.py:15-48`

**建议修改方案**:

```python
@dataclass(frozen=True)
class LightweightMigration:
    version: str
    description: str
    apply: MigrationApply
    rollback: MigrationApply | None = None  # 可选的回滚函数

async def rollback_migration(conn, migration: LightweightMigration) -> None:
    if migration.rollback is None:
        raise ValueError(f"Migration {migration.version} does not support rollback")
    await migration.rollback(conn)
    await conn.execute(
        text("DELETE FROM schema_migrations WHERE version = :version"),
        {"version": migration.version}
    )
```

为关键迁移添加回滚：

```python
async def _rollback_adapter_current_robot_id(conn) -> None:
    adapter_columns = await _table_columns(conn, "adapters")
    if "current_robot_id" in adapter_columns:
        await conn.exec_driver_sql("ALTER TABLE adapters DROP COLUMN current_robot_id")

LightweightMigration(
    "20260705_001_adapter_current_robot_id",
    "Add adapters.current_robot_id",
    _add_adapter_current_robot_id,
    _rollback_adapter_current_robot_id,  # 回滚函数
)
```

**验收标准**:

- 关键迁移可回滚
- 回滚后数据库恢复到迁移前状态

**推荐测试命令**:

```python
pytest tests/test_migration_rollback.py -v
```

**适合便宜模型执行**: 是

---

### P2-10: 导出包未压缩，大量消息时文件过大

**类型**: 性能/用户体验  
**优先级**: P2

**问题描述**:  
`app/services/backup_service.py` 导出的 JSON 包：

1. 未压缩，大量消息时可达数百 MB
2. 媒体文件 base64 编码增加 33% 体积
3. 网络传输慢

**影响范围**:

- `app/services/backup_service.py` (导出函数)
- `app/api.py:1318-1350` (导出 API)

**建议修改方案**:

```python
import gzip
import json

async def export_package_compressed(...) -> bytes:
    package = await export_package(...)
    json_str = json.dumps(package, ensure_ascii=False, separators=(",", ":"))
    return gzip.compress(json_str.encode("utf-8"), compresslevel=6)

# API 返回压缩包
@router.get("/export")
async def export_data(...):
    compressed = await BackupService.export_package_compressed(...)
    return Response(
        content=compressed,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="backup_{int(time.time())}.json.gz"',
        }
    )
```

**验收标准**:

- 导出文件体积减少 70-90%
- 导入兼容 `.json` 和 `.json.gz`

**推荐测试命令**:

```python
pytest tests/test_backup_service.py::test_export_compressed -v
```

**适合便宜模型执行**: 是

---

### P2-11: 前端路由缺失，刷新后状态丢失

**类型**: 用户体验  
**优先级**: P2

**问题描述**:  
前端是单页应用，但：

1. 没有路由管理（无 URL 状态）
2. 刷新后当前账号、会话、消息位置全部丢失
3. 无法分享特定会话链接

**影响范围**:

- `app/static/assets/app.js` (整个前端)
- `app/static/index.html`

**建议修改方案**:  
使用 URL hash 或 History API 保存状态：

```javascript
// 简单方案：使用 hash
const saveState = () => {
  const hash = `#robot=${state.currentRobot?.id || ''}&room=${state.currentRoom?.room_id || ''}`;
  window.location.hash = hash;
};

const restoreState = () => {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const robotId = params.get('robot');
  const roomId = params.get('room');
  if (robotId) {
    // 恢复账号和会话
  }
};

window.addEventListener('hashchange', restoreState);
window.addEventListener('load', restoreState);
```

**验收标准**:

- 刷新后保持当前账号和会话
- 可复制 URL 分享特定会话

**推荐测试命令**:  
手动测试浏览器前进后退

**适合便宜模型执行**: 是

---

### P2-12: 缺少健康检查依赖项检测

**类型**: 部署/可观测性  
**优先级**: P2

**问题描述**:  
`/health` 端点只返回固定 JSON，不检查：

1. 数据库连接是否正常
2. 存储目录是否可写
3. FFmpeg 是否可用（如果启用）

容器虽然健康但实际功能不可用。

**影响范围**:

- `app/main.py:106-108`

**建议修改方案**:

```python
from sqlalchemy import text

@app.get("/health", response_model=HealthResponse)
async def health(db: AsyncSession = Depends(get_db_session)) -> HealthResponse:
    checks = {"app": "ok", "database": "unknown", "storage": "unknown"}
    
    # 数据库检查
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    
    # 存储检查
    try:
        settings = get_settings()
        test_file = settings.storage_root / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()
        checks["storage"] = "ok"
    except Exception:
        checks["storage"] = "error"
    
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return HealthResponse(status=overall, app=settings.app_name, checks=checks)
```

**验收标准**:

- 数据库断开时健康检查返回 503
- 存储不可写时返回降级状态

**推荐测试命令**:

```python
pytest tests/test_health.py::test_health_check_dependencies -v
```

**适合便宜模型执行**: 是

---

### P2-13: 审计日志未脱敏敏感信息

**类型**: 安全/合规  
**优先级**: P2

**问题描述**:  
`app/services/audit_log_service.py` 将操作详情完整写入 `audit_logs.detail_json`，可能包含：

- 密码明文（用户创建/重置密码）
- Token 明文（Token 创建/轮换）
- 导入包中的敏感内容

**影响范围**:

- `app/services/audit_log_service.py:10-35`
- `app/api.py` (所有 `_audit()` 调用)

**建议修改方案**:

```python
SENSITIVE_KEYS = {"password", "token", "secret", "key", "authorization"}

def sanitize_detail(detail: dict | None) -> dict | None:
    if detail is None:
        return None
    sanitized = {}
    for key, value in detail.items():
        if any(sensitive in key.lower() for sensitive in SENSITIVE_KEYS):
            sanitized[key] = "***REDACTED***"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_detail(value)
        else:
            sanitized[key] = value
    return sanitized

class AuditLogService:
    @staticmethod
    async def log_action(..., detail: dict | None = None, ...):
        sanitized_detail = sanitize_detail(detail)
        # ...
```

**验收标准**:

- 审计日志中不含密码/Token 明文
- 可审计操作类型和结果

**推荐测试命令**:

```python
pytest tests/test_audit_api.py::test_audit_log_sanitization -v
```

**适合便宜模型执行**: 是

---

### P2-14: Docker 镜像体积较大

**类型**: 部署/成本  
**优先级**: P2

**问题描述**:  
`Dockerfile` 使用 `python:3.11-slim` 基础镜像，但：

1. 未清理 pip 缓存（虽然设置了 `PIP_NO_CACHE_DIR`）
2. 未使用多阶段构建
3. 包含不必要的开发依赖（pytest）

**影响范围**:

- `Dockerfile`
- `requirements.txt`

**建议修改方案**:

```dockerfile
# 多阶段构建
FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt ./
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/root/.local/bin:$PATH

WORKDIR /app

# 只复制运行时依赖
COPY --from=builder /root/.local /root/.local
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY data/storage/.gitkeep ./data/storage/.gitkeep
COPY data/backups/.gitkeep ./data/backups/.gitkeep

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)))" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

拆分 requirements:

```
# requirements-prod.txt
fastapi==0.116.1
uvicorn[standard]==0.35.0
SQLAlchemy==2.0.43
alembic==1.16.5
aiosqlite==0.21.0
asyncpg==0.30.0
pydantic-settings==2.10.1
httpx==0.28.1

# requirements-dev.txt
-r requirements-prod.txt
pytest==8.4.1
pytest-asyncio==1.1.0
```

**验收标准**:

- 镜像体积减少 30-50%
- 生产镜像不含测试依赖

**推荐测试命令**:

```bash
docker build -t chat-audit-core:optimized .
docker images | grep chat-audit-core
```

**适合便宜模型执行**: 是

---

### P2-15: 前端错误处理不友好

**类型**: 用户体验  
**优先级**: P2

**问题描述**:  
`app/static/assets/app.js` 的错误处理：

```javascript
pushUiLog(`${method} ${url} 失败：${message}`, 'error');
throw new Error(message);
```

问题：

1. 错误抛出后中断后续逻辑，用户无法继续操作
2. 错误信息不够友好（显示技术细节）
3. 无重试机制

**影响范围**:

- `app/static/assets/app.js:89-107`

**建议修改方案**:

```javascript
const requestJson = async (url, options = {}, retries = 2) => {
  const withAuth = { ...options, headers: authHeaders(options.headers || {}) };
  const method = options.method || 'GET';
  
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      let response = await fetch(url, withAuth);
      
      if (response.status === 401 && attempt === 0 && promptForAdminApiToken()) {
        response = await fetch(url, { ...options, headers: authHeaders(options.headers || {}) });
      }
      
      if (!response.ok) {
        const message = await responseErrorMessage(response, url);
        
        // 用户友好的错误信息
        const userMessage = response.status === 403 
          ? '权限不足，请检查 Token 角色'
          : response.status >= 500
          ? '服务器错误，请稍后重试'
          : message;
        
        pushUiLog(`${method} ${url} 失败：${userMessage}`, 'error');
        
        // 5xx 错误且还有重试次数，等待后重试
        if (response.status >= 500 && attempt < retries) {
          await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
          continue;
        }
        
        throw new Error(userMessage);
      }
      
      pushUiLog(`${method} ${url} · ${response.status}`);
      if (response.status === 204) return null;
      return await response.json();
      
    } catch (err) {
      if (attempt === retries) throw err;
      await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
    }
  }
};
```

**验收标准**:

- 临时网络错误自动重试
- 错误信息用户可理解
- 错误不阻塞后续操作

**推荐测试命令**:  
手动测试网络异常场景

**适合便宜模型执行**: 是

---

### P3-01: 缺少 API 文档示例和说明

**类型**: 文档  
**优先级**: P3

**问题描述**:  
FastAPI 自动生成的 `/docs` 缺少：

1. 请求体示例
2. 响应示例
3. 各个 API 的使用场景说明

**影响范围**:

- `app/api.py` (所有端点)
- `app/schemas.py`

**建议修改方案**:

```python
from pydantic import Field

class MessageIngestRequest(BaseModel):
    """外部消息接入请求体。
    
    用于接收来自微信 Hook 或其他消息源的消息。
    """
    robot_id: str = Field(..., description="机器人账号标识", example="wx_bot_001")
    platform: str = Field(..., description="平台类型：qq/wechat", example="wechat")
    # ...
    
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "robot_id": "wx_bot_001",
                    "platform": "wechat",
                    "room_id": "group_12345",
                    "message_type": "group",
                    "sender_id": "user_67890",
                    "raw_message": "你好，这是一条测试消息",
                    "timestamp": 1704067200
                }
            ]
        }
    }

@router.post(
    "/receive_external_msg",
    response_model=MessageIngestResponse,
    summary="接收外部消息",
    description="""
    接收来自微信 Hook、第三方机器人等外部消息源的消息。
    
    **使用场景**：
    - 微信 PC Hook 推送的消息
    - 自定义采集器发送的消息
    
    **权限要求**：需要管理员 Token
    """,
    responses={
        200: {
            "description": "消息接收成功",
            "content": {
                "application/json": {
                    "example": {"msg_hash": "a1b2c3d4e5f6..."}
                }
            }
        },
        400: {"description": "请求参数错误"},
        401: {"description": "未授权"},
    }
)
async def receive_external_message(...):
    # ...
```

**验收标准**:

- `/docs` 中每个 API 都有清晰说明和示例
- 新用户可通过文档快速上手

**推荐测试命令**:  
手动访问 `/docs` 检查

**适合便宜模型执行**: 是

---

### P3-02: 缺少性能监控和指标导出

**类型**: 可观测性  
**优先级**: P3

**问题描述**:  
无法监控：

1. API 响应时间
2. 数据库查询耗时
3. 媒体下载成功率
4. WebSocket 连接数

**影响范围**:

- 整个应用

**建议修改方案**:  
集成 Prometheus：

```python
from prometheus_client import Counter, Histogram, Gauge, generate_latest

# 定义指标
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"]
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"]
)

websocket_connections = Gauge(
    "websocket_connections",
    "Active WebSocket connections"
)

media_download_total = Counter(
    "media_download_total",
    "Total media downloads",
    ["media_type", "status"]
)

# 中间件
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    
    http_requests_total.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code
    ).inc()
    
    http_request_duration_seconds.labels(
        method=request.method,
        endpoint=request.url.path
    ).observe(duration)
    
    return response

# Metrics 端点
@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type="text/plain")
```

添加依赖：

```
prometheus-client==0.20.0
```

**验收标准**:

- Prometheus 可抓取 `/metrics`
- Grafana 可展示关键指标

**推荐测试命令**:

```bash
curl http://localhost:8000/metrics
```

**适合便宜模型执行**: 是

---

### P3-03: 缺少 WebSocket 心跳机制

**类型**: 稳定性  
**优先级**: P3

**问题描述**:  
OneBot WebSocket 连接：

1. 无心跳检测，连接断开不能及时发现
2. 中间网络设备（NAT/防火墙）可能因空闲超时关闭连接
3. NapCat 重启后需要手动重连

**影响范围**:

- `app/ws.py:143-214`

**建议修改方案**:

```python
async def onebot11_reverse_ws(...):
    # ...
    ping_interval = 30  # 30 秒心跳
    last_pong = time.time()
    
    async def ping_task():
        nonlocal last_pong
        while True:
            await asyncio.sleep(ping_interval)
            try:
                pong_waiter = await websocket.ping()
                await asyncio.wait_for(pong_waiter, timeout=10)
                last_pong = time.time()
            except asyncio.TimeoutError:
                logger.warning(f"WebSocket ping timeout for {adapter_id}")
                break
            except Exception:
                break
    
    background_tasks.add(asyncio.create_task(ping_task()))
    
    try:
        async for message in websocket.iter_text():
            # ...
    except WebSocketDisconnect:
        return
    finally:
        # ...
```

**验收标准**:

- 连接断开后 40 秒内检测到
- 正常连接不受影响

**推荐测试命令**:  
手动断开网络，观察日志

**适合便宜模型执行**: 是

---

### P3-04: 前端未做离线缓存

**类型**: 用户体验  
**优先级**: P3

**问题描述**:  
前端资源（JS/CSS）未使用 Service Worker 缓存：

1. 刷新页面需要重新下载
2. 弱网环境加载慢

**影响范围**:

- `app/static/` 目录

**建议修改方案**:  
添加 Service Worker：

```javascript
// app/static/sw.js
const CACHE_NAME = 'chat-audit-v1';
const urlsToCache = [
  '/',
  '/assets/app.js',
  '/assets/app.css',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(urlsToCache))
  );
});

self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      return response || fetch(event.request);
    })
  );
});
```

在 `index.html` 注册：

```html
<script>
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js');
  }
</script>
```

**验收标准**:

- 首次加载后，离线可访问前端
- API 数据仍需在线

**推荐测试命令**:  
浏览器开发者工具 -> Application -> Service Workers

**适合便宜模型执行**: 是

---

### P3-05: 缺少数据库索引优化

**类型**: 性能  
**优先级**: P3

**问题描述**:  
当前索引覆盖不足：

1. `messages` 表缺少 `(platform, room_id, timestamp)` 联合索引
2. `robot_messages` 表缺少 `(robot_id, msg_hash)` 覆盖索引（虽然有唯一约束）
3. 频繁查询的字段如 `message_type`、`sender_id` 无索引

**影响范围**:

- `app/models.py`

**建议修改方案**:

```python
class Message(Base):
    __tablename__ = "messages"
    
    # ... 现有字段 ...
    
    __table_args__ = (
        Index("idx_room_timestamp", "room_id", "timestamp"),
        Index("idx_platform_room_timestamp", "platform", "room_id", "timestamp"),
        Index("idx_sender_timestamp", "sender_id", "timestamp"),
        Index("idx_message_type_timestamp", "message_type", "timestamp"),
    )
```

**验收标准**:

- 查询 10 万条消息时响应时间 < 100ms

**推荐测试命令**:

```sql
EXPLAIN ANALYZE SELECT * FROM messages WHERE platform = 'qq' AND room_id = '12345' AND timestamp > 1700000000 ORDER BY timestamp DESC LIMIT 50;
```

**适合便宜模型执行**: 是

---

### P3-06: 测试覆盖率不足

**类型**: 测试  
**优先级**: P3

**问题描述**:  
当前 165 个测试用例，但缺少：

1. 并发场景测试（WebSocket 同时连接、API 并发请求）
2. 边界条件测试（空消息、超长消息、特殊字符）
3. 故障注入测试（数据库断开、网络超时、磁盘满）
4. 端到端测试（完整消息流程）

**影响范围**:

- `tests/` 目录

**建议修改方案**:  
添加测试类别：

```python
# tests/test_concurrent.py
import asyncio
import pytest

@pytest.mark.asyncio
async def test_concurrent_message_ingestion():
    """测试 100 并发消息入库."""
    tasks = [
        send_message(robot_id="test", message=f"msg_{i}")
        for i in range(100)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, str) for r in results)  # 所有消息都返回 msg_hash

# tests/test_edge_cases.py
@pytest.mark.asyncio
async def test_empty_message():
    """测试空消息."""
    result = await process_message(raw_message="")
    assert result is not None

@pytest.mark.asyncio
async def test_oversized_message():
    """测试超长消息（100KB）."""
    huge_message = "A" * 100_000
    result = await process_message(raw_message=huge_message)
    assert result is not None

# tests/test_fault_injection.py
@pytest.mark.asyncio
async def test_database_disconnect_recovery():
    """测试数据库断开后恢复."""
    # 模拟数据库断开
    # 验证重连机制

@pytest.mark.asyncio
async def test_media_download_timeout():
    """测试媒体下载超时."""
    # 模拟超时
    # 验证降级逻辑
```

添加覆盖率工具：

```bash
pip install pytest-cov
pytest --cov=app --cov-report=html tests/
```

**验收标准**:

- 代码覆盖率 > 80%
- 关键路径覆盖率 > 95%

**推荐测试命令**:

```bash
pytest --cov=app --cov-report=term-missing tests/
```

**适合便宜模型执行**: 是

---

### P3-07: 缺少 Docker Compose 健康检查依赖

**类型**: 部署  
**优先级**: P3

**问题描述**:  
`docker-compose.yml` 中 `app` 服务虽然依赖 `postgres` 健康检查，但：

1. `postgres` 健康检查只检查连接，不检查数据库是否创建
2. `app` 启动后立即运行迁移，可能失败

**影响范围**:

- `docker-compose.yml`

**建议修改方案**:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB} && psql -U $${POSTGRES_USER} -d $${POSTGRES_DB} -c 'SELECT 1'"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

  app:
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import json, urllib.request; r = json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); exit(0 if r.get('status') == 'ok' else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
```

**验收标准**:

- `docker-compose up` 后服务稳定可用
- 依赖项未就绪时不启动应用

**推荐测试命令**:

```bash
docker-compose down -v && docker-compose up -d
docker-compose ps  # 检查所有服务 healthy
```

**适合便宜模型执行**: 是

---

### P3-08: 前端代码未压缩和混淆

**类型**: 性能/安全  
**优先级**: P3

**问题描述**:  
`app/static/assets/app.js` 是未压缩的源码：

1. 文件体积大（2678 行）
2. 包含调试代码和注释
3. 变量名可读，容易被逆向

**影响范围**:

- `app/static/assets/`

**建议修改方案**:  
使用构建工具：

```bash
# 安装 terser
npm install -g terser

# 压缩 JS
terser app/static/assets/app.js \
  --compress \
  --mangle \
  --output app/static/assets/app.min.js

# 压缩 CSS
npm install -g csso-cli
csso app/static/assets/app.css \
  --output app/static/assets/app.min.css
```

或使用 Vite/Webpack 构建：

```javascript
// vite.config.js
import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    outDir: 'app/static/assets',
    rollupOptions: {
      input: 'src/app.js',
      output: {
        entryFileNames: 'app.min.js',
        assetFileNames: 'app.min.css',
      },
    },
    minify: 'terser',
  },
});
```

**验收标准**:

- JS 文件体积减少 60-70%
- 功能不受影响

**推荐测试命令**:

```bash
ls -lh app/static/assets/
# 对比压缩前后体积
```

**适合便宜模型执行**: 是

---

### P3-09: 缺少速率限制说明和监控

**类型**: 文档/可观测性  
**优先级**: P3

**问题描述**:  
`HIGH_RISK_RATE_LIMIT_PER_MINUTE=10` 限流机制：

1. 未在文档中说明哪些接口被限流
2. 触发限流时前端不友好提示
3. 无法监控限流触发频率

**影响范围**:

- `app/api.py:78-86` (限流实现)
- README 文档

**建议修改方案**:

```python
# 添加 Prometheus 指标
rate_limit_exceeded = Counter(
    "rate_limit_exceeded_total",
    "Rate limit exceeded events",
    ["action", "actor"]
)

def _enforce_high_risk_rate_limit(...):
    # ... 现有逻辑 ...
    if len(window) >= limit:
        rate_limit_exceeded.labels(action=action, actor=actor).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded for {action}: max {limit} requests per minute",
            headers={"Retry-After": "60"},
        )
```

前端处理 429：

```javascript
if (response.status === 429) {
  const retryAfter = response.headers.get('Retry-After') || '60';
  pushUiLog(`操作过于频繁，请 ${retryAfter} 秒后重试`, 'warning');
  return;
}
```

文档补充：

```markdown
## 接口限流

以下高风险操作受限流保护（默认 10 次/分钟）：
- 导入数据 `POST /api/import`
- 运行备份 `POST /api/backup/run`
- 导出数据 `GET /api/export`
- 强制下线会话 `DELETE /api/auth/sessions/{session_id}`
- 删除适配器 `DELETE /api/adapters/{adapter_id}`

触发限流时返回 429，请根据 `Retry-After` header 等待后重试。
```

**验收标准**:

- 文档清晰说明限流策略
- 前端友好提示限流
- 可监控限流触发

**推荐测试命令**:

```bash
# 快速触发限流
for i in {1..15}; do curl -X POST http://localhost:8000/api/backup/run -H "Authorization: Bearer $TOKEN"; done
```

**适合便宜模型执行**: 是

---

### P3-10: 缺少数据备份恢复演练文档

**类型**: 文档/运维  
**优先级**: P3

**问题描述**:  
备份功能已实现，但缺少：

1. 灾难恢复步骤
2. 备份恢复演练指南
3. RTO/RPO 说明

**影响范围**:

- 文档

**建议修改方案**:  
添加 `DISASTER_RECOVERY.md`：

````markdown
# 灾难恢复指南

## 备份策略

- **自动备份**: 每天凌晨 3 点（可配置 `AUTO_BACKUP_CRON`）
- **保留策略**: 最近 7 份（可配置 `AUTO_BACKUP_KEEP_LATEST`）
- **备份内容**: 消息、媒体索引、配置、资料缓存
- **媒体文件**: 单独存储在 `data/storage/`

## 恢复目标

- **RTO (恢复时间目标)**: < 1 小时
- **RPO (恢复点目标)**: 最多丢失 24 小时数据

## 完整恢复步骤

### 1. 准备新环境
```bash
# 部署应用
docker-compose up -d

# 等待服务启动
docker-compose ps
````

### 2. 恢复数据库

```bash
# 停止应用
docker-compose stop app

# 从最新备份恢复
cat data/backups/backup_*.json | \
  curl -X POST http://localhost:8000/api/import \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d @-

# 重启应用
docker-compose start app
```

### 3. 恢复媒体文件

```bash
# 从备份恢复 storage 目录
tar -xzf storage_backup.tar.gz -C data/storage/
```

### 4. 验证恢复

```bash
# 检查消息数量
curl http://localhost:8000/api/dashboard -H "Authorization: Bearer $TOKEN"

# 检查离线完整性
curl http://localhost:8000/api/offline/audit -H "Authorization: Bearer $TOKEN"
```

## 定期演练

**建议频率**: 每季度

**演练步骤**:

1. 在测试环境执行完整恢复
2. 验证数据完整性
3. 记录实际 RTO
4. 更新恢复文档

## 常见问题

### Q: 备份文件签名验证失败？

A: 检查 `APP_SECRET_KEY` 和 `SYSTEM_INSTANCE_ID` 是否与备份时一致。

### Q: 导入后消息数量不对？

A: 检查 `messages` 表和 `robot_messages` 表的记录数。

### Q: 媒体文件显示不出来？

A: 检查 `storage_root` 路径和 `public_storage_prefix` 配置。

````

**验收标准**:  
- 按文档可成功恢复数据
- 演练记录可追溯

**推荐测试命令**:  
按文档执行一次完整恢复演练

**适合便宜模型执行**: 是

---

### P3-11: 缺少 CI/CD 配置
**类型**: 工程化  
**优先级**: P3

**问题描述**:  
项目没有 CI/CD 配置：
1. 提交前无自动测试
2. 代码风格检查依赖手动
3. Docker 镜像构建手动

**影响范围**:  
- 整个仓库

**建议修改方案**:  
添加 `.github/workflows/test.yml`（GitHub Actions）：
```yaml
name: Test

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: test_db
          POSTGRES_USER: test_user
          POSTGRES_PASSWORD: test_password
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install ruff black pytest-cov
      
      - name: Lint with ruff
        run: ruff check app/ tests/
      
      - name: Format check with black
        run: black --check app/ tests/
      
      - name: Run tests
        env:
          DATABASE_URL: postgresql+asyncpg://test_user:test_password@localhost:5432/test_db
        run: |
          pytest --cov=app --cov-report=xml tests/
      
      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          file: ./coverage.xml

  build:
    runs-on: ubuntu-latest
    needs: test
    
    steps:
      - uses: actions/checkout@v3
      
      - name: Build Docker image
        run: docker build -t chat-audit-core:${{ github.sha }} .
      
      - name: Test Docker image
        run: |
          docker run -d -p 8000:8000 --name test-container chat-audit-core:${{ github.sha }}
          sleep 10
          curl --fail http://localhost:8000/health
          docker stop test-container
````

或 Forgejo Actions（兼容 GitHub Actions）：

```yaml
# .forgejo/workflows/test.yml
# 类似配置
```

**验收标准**:

- 每次推送自动运行测试
- 测试失败阻止合并
- 可查看覆盖率报告

**推荐测试命令**:  
推送代码后查看 Actions 运行结果

**适合便宜模型执行**: 是

---

### P3-12: 缺少贡献指南和开发者文档

**类型**: 文档  
**优先级**: P3

**问题描述**:  
项目缺少：

1. 贡献指南（`CONTRIBUTING.md`）
2. 架构设计文档
3. 代码规范说明
4. 开发环境搭建指南

**影响范围**:

- 文档

**建议修改方案**:  
添加 `CONTRIBUTING.md`：

````markdown
# 贡献指南

感谢你对本项目的关注！

## 开发环境

### 要求
- Python 3.11+
- PostgreSQL 16+ (可选，开发可用 SQLite)
- Git

### 本地搭建
```bash
# 克隆仓库
git clone http://192.168.31.210:18085/YokiiroBW/chat-audit-core.git
cd chat-audit-core

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env

# 运行开发服务器
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
````

## 代码规范

- **Python**: 遵循 PEP 8，使用 `black` 格式化，`ruff` 检查
- **命名**:
    - 文件/模块: `snake_case`
    - 类: `PascalCase`
    - 函数/变量: `snake_case`
    - 常量: `UPPER_SNAKE_CASE`
- **注释**: 关键逻辑必须有注释，公共函数必须有 docstring
- **提交信息**: 中文，清晰描述变更内容

## 提交流程

1. Fork 仓库并创建功能分支
2. 编写代码和测试
3. 运行测试确保通过：`pytest tests/ -v`
4. 格式化代码：`black app/ tests/`
5. 检查代码：`ruff check app/ tests/`
6. 提交并推送
7. 创建 Pull Request

## 测试要求

- 新功能必须有测试
- Bug 修复必须有回归测试
- 测试覆盖率不低于 80%

## 架构概览

```
app/
├── main.py           # 应用入口
├── config.py         # 配置管理
├── database.py       # 数据库连接与迁移
├── models.py         # ORM 模型
├── schemas.py        # Pydantic 模型
├── api.py            # HTTP API 路由
├── ws.py             # WebSocket 路由
├── adapters/         # 平台适配器
├── services/         # 业务逻辑层
└── static/           # 前端资源
```

## 发布流程

1. 更新版本号
2. 更新 CHANGELOG
3. 标记 Git tag
4. 推送到 Forgejo
5. 构建 Docker 镜像
6. 部署到 NAS

## 联系方式

- Issue: http://192.168.31.210:18085/YokiiroBW/chat-audit-core/issues
- 项目负责人: [待填写]

````

添加 `ARCHITECTURE.md`：
```markdown
# 架构设计

## 总体架构

````

┌─────────────┐ │ NapCat │ │ (OneBot) │ └──────┬──────┘ │ WebSocket ↓ ┌─────────────────────────────────────┐ │ chat-audit-core │ │ ┌───────────────────────────────┐ │ │ │ WebSocket Handler (ws.py) │ │ │ └─────────────┬─────────────────┘ │ │ ↓ │ │ ┌───────────────────────────────┐ │ │ │ Services Layer │ │ │ │ - MessageService │ │ │ │ - MediaService │ │ │ │ - CapturePolicyService │ │ │ └─────────────┬─────────────────┘ │ │ ↓ │ │ ┌───────────────────────────────┐ │ │ │ Database (PostgreSQL) │ │ │ │ - messages (全局消息池) │ │ │ │ - robot_messages (主视角) │ │ │ │ - media_assets (媒体索引) │ │ │ └───────────────────────────────┘ │ │ │ │ ┌───────────────────────────────┐ │ │ │ Storage (本地文件) │ │ │ │ - 内容寻址媒体池 │ │ │ │ - 头像缓存 │ │ │ │ - 卡片快照 │ │ │ └───────────────────────────────┘ │ └─────────────────────────────────────┘ │ HTTP API ↓ ┌─────────────┐ │ Web 前端 │ │ (Vue-less) │ └─────────────┘

```

## 核心设计原则

### 1. 主视角隔离
同一条群消息可被多个机器人账号看到，但查询时按 `robot_id` 做视角切片。

### 2. 全局消息池去重
`msg_hash = MD5(platform + room_id + sender_id + raw_message)`

### 3. 内容寻址媒体存储
媒体文件以内容 MD5 命名并复用，避免重复落盘。

### 4. 游标滚动加载
聊天历史使用 `before_timestamp + limit` 向上滚动加载。

## 数据流

### 消息入库流程
1. NapCat 通过 WebSocket 推送 OneBot 事件
2. `onebot11.normalize_message_event` 规范化为内部格式
3. `CapturePolicyService.should_capture` 检查抓取策略
4. `MediaService.rewrite_cq_media_to_local_paths` 下载媒体
5. 计算 `msg_hash`，写入 `messages` 表（去重）
6. 写入 `robot_messages` 关联表（主视角）
7. 后台任务拉取头像、群资料、合并转发详情

### 媒体缓存流程
1. 解析 CQ 码中的 `url` 参数
2. 下载媒体文件（带超时和大小限制）
3. 可选 FFmpeg 转码
4. 计算 MD5 作为文件名
5. 写入 `data/storage/`
6. 更新 `media_assets` 索引
7. 替换 CQ 码为本地路径

## 扩展点

- **新平台接入**: 实现 `adapters/` 下的规范化函数
- **新媒体类型**: 扩展 `MediaService` 的 CQ 解析
- **自定义抓取策略**: 修改 `CapturePolicyService`

## 性能考虑

- **数据库连接池**: 20 + 10 overflow
- **媒体下载并发**: `asyncio.gather` 批量下载
- **前端分页**: 50 条/页，虚拟滚动
```

**验收标准**:

- 新贡献者可按文档完成首次提交
- 架构图清晰易懂

**推荐测试命令**:  
邀请其他开发者测试文档

**适合便宜模型执行**: 是

---

## 三、可执行开发队列（按优先级排序）

### 立即执行（P1，8 项）

1. **P1-01**: WebSocket 连接并发保护（3 天，需要仔细设计）
2. **P1-05**: 密码哈希升级为 bcrypt（1 天 + 数据迁移）
3. **P1-02**: 媒体下载白名单验证（1 天）
4. **P1-03**: 数据库连接池配置（0.5 天）
5. **P1-06**: SQL 注入风险修复（0.5 天）
6. **P1-04**: 后台任务异常捕获（1 天）
7. **P1-07**: 媒体回填批量提交优化（1 天）
8. **P1-08**: CSRF 防护（2 天，需要前后端配合）

### 近期执行（P2，15 项）

9. **P2-01**: 环境变量验证完善（0.5 天）
10. **P2-03**: API 请求大小限制（0.5 天）
11. **P2-07**: FFmpeg 转码超时（0.5 天）
12. **P2-08**: 合并转发深度限制（0.5 天）
13. **P2-02**: 时区处理统一（1 天）
14. **P2-04**: 备份文件名随机后缀（0.5 天）
15. **P2-05**: 前端 XSS 防护（1 天）
16. **P2-06**: 结构化日志（1 天）
17. **P2-12**: 健康检查依赖项检测（0.5 天）
18. **P2-13**: 审计日志脱敏（0.5 天）
19. **P2-09**: 数据库迁移回滚机制（1 天）
20. **P2-10**: 导出包压缩（0.5 天）
21. **P2-11**: 前端路由（1 天）
22. **P2-14**: Docker 镜像优化（0.5 天）
23. **P2-15**: 前端错误处理优化（1 天）

### 后续优化（P3，12 项）

24. **P3-01**: API 文档完善（1 天）
25. **P3-02**: Prometheus 指标导出（1 天）
26. **P3-03**: WebSocket 心跳机制（0.5 天）
27. **P3-05**: 数据库索引优化（0.5 天）
28. **P3-06**: 测试覆盖率提升（3 天）
29. **P3-07**: Docker Compose 健康检查（0.5 天）
30. **P3-08**: 前端代码压缩（0.5 天）
31. **P3-09**: 速率限制监控（0.5 天）
32. **P3-04**: 前端离线缓存（1 天）
33. **P3-10**: 灾难恢复文档（1 天）
34. **P3-11**: CI/CD 配置（1 天）
35. **P3-12**: 贡献指南文档（1 天）

---

## 四、总体评估

### 优点 ✅

1. **架构设计清晰**: 主视角隔离、全局消息池、内容寻址存储等核心设计理念先进
2. **测试覆盖较好**: 165 个测试用例通过，关键路径有测试
3. **功能完整**: QQ 消息备份、离线审计、导出导入、角色抓取策略、管理鉴权等核心功能已实现
4. **文档齐全**: README、交接文档、队列文档等齐全
5. **代码规范**: 遵循 FastAPI/SQLAlchemy 最佳实践，代码可读性好
6. **部署友好**: Docker Compose、Alembic 迁移、健康检查等生产就绪

### 主要风险 ⚠️

1. **并发安全问题**: WebSocket 连接管理、数据库操作存在竞态条件
2. **安全性不足**: 密码哈希、CSRF、输入验证、速率限制需要加固
3. **可观测性欠缺**: 日志、监控、追踪不足，生产问题排查困难
4. **容错能力弱**: 后台任务失败静默、数据库死锁风险、网络超时处理不完善

### 建议优先级 📋

**第一阶段（2 周）**: 修复 P1 级别安全和并发问题，确保系统稳定可用  
**第二阶段（3 周）**: 完善 P2 级别可观测性和容错能力，提升生产就绪度  
**第三阶段（4 周）**: 优化 P3 级别用户体验和工程化，提升团队协作效率

### 代码质量评分 📊

- **功能完整性**: 9/10（核心功能齐全，微信路线封存合理）
- **代码质量**: 8/10（规范清晰，但缺少类型注解）
- **测试覆盖**: 7/10（有测试，但缺少边界和故障场景）
- **安全性**: 6/10（有基础鉴权，但存在密码哈希、CSRF、XSS 等问题）
- **性能**: 7/10（设计合理，但缺少连接池、索引优化）
- **可观测性**: 5/10（审计日志可用，但缺少应用日志和监控）
- **部署就绪**: 8/10（Docker 化完善，但缺少 CI/CD 和演练）
- **文档完善**: 8/10（文档齐全，但缺少架构设计和贡献指南）

**总体评分**: **7.25/10**（良好，可投入生产，但需持续改进）

---

## 五、补充说明

### 未审计项

由于是只读审计且时间有限，以下方面未深入审计：

1. **前端完整逻辑**: 仅审查了前 300 行 JS 代码，完整的 2678 行未全部审查
2. **所有测试用例**: 未逐个审查 165 个测试的质量
3. **性能基准测试**: 未实际测试高并发、大数据量场景
4. **Alembic 迁移文件**: 仅审查了轻量迁移，未检查所有 Alembic 版本
5. **微信封存代码**: `wechat_tray_adapter/` 和微信相关代码未深入审查
6. **NAS 实际运行状态**: 基于文档推断，未连接 NAS 实际验证

### 审计方法

1. **静态代码审查**: 阅读所有核心代码文件
2. **架构分析**: 理解项目设计和数据流
3. **安全检查**: OWASP Top 10、常见漏洞模式
4. **最佳实践对比**: FastAPI、SQLAlchemy、Docker 最佳实践
5. **文档完整性**: 检查文档与代码一致性

### 执行建议

1. **按优先级推进**: 先 P1，再 P2，最后 P3
2. **每个发现项独立提交**: 便于代码审查和回滚
3. **提交前测试**: 运行相关测试和必要的全量测试
4. **更新文档**: 代码变更后更新相关文档
5. **NAS 验收**: 涉及部署行为的变更需在 NAS 验收
6. **推送 Forgejo**: 每个可验收版本都推送

### 便宜模型适合执行的项（23/35）

以下发现项**适合交给便宜模型执行代码修改**（标准化、明确、低风险）：

- P1: 02, 03, 04, 06, 07
- P2: 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 12, 13, 14, 15
- P3: 01, 02, 03, 05, 07, 08, 09, 10, 11, 12

以下发现项**不适合交给便宜模型**（需要架构决策、安全权衡）：

- P1: 01 (并发设计), 05 (数据迁移策略), 08 (CSRF 方案选择)
- P2: 11 (路由方案选择)
- P3: 04 (Service Worker 实现), 06 (性能优化策略)

### 长期改进方向

1. **微服务拆分**: 当消息量超过千万级时，考虑拆分消息入库、媒体处理、API 查询为独立服务
2. **消息队列**: 引入 Redis/RabbitMQ 处理后台任务，替代 `asyncio.create_task`
3. **CDN 加速**: 媒体文件接入 CDN，减轻本地存储压力
4. **全文搜索**: 引入 Elasticsearch 提供更强大的搜索能力
5. **机器学习**: 对消息内容做敏感信息识别、自动分类、情感分析
6. **多实例部署**: 支持水平扩展，使用 Redis 做会话存储和分布式锁

---

## 六、审计结论

**chat-audit-core** 是一个**架构清晰、功能完整、代码规范**的 QQ 社交资产审计系统。核心的消息备份、离线审计、角色抓取策略、导出导入等功能已经实现并通过测试，可以投入生产使用。

主要问题集中在**并发安全、密码安全、CSRF 防护**等安全加固方面，以及**日志监控、容错能力**等生产就绪度方面。这些问题都有明确的修复方案，建议在 2 周内完成 P1 级别修复后再大规模部署。

项目的**主视角隔离**、**内容寻址媒体存储**、**离线优先**等设计理念先进，为后续扩展到微信等其他平台打下了良好基础。微信路线的封存决策合理，符合当前技术约束和风险考量。

建议继续按照现有的开发流程（测试 → 提交 → 推送 → 部署 → 验收）推进剩余队列，同时补充**结构化日志**、**监控告警**、**CI/CD**等生产化能力，最终达到企业级可靠性标准。

---

**审计完成**  
**发现项总数**: 35 项（P0: 0, P1: 8, P2: 15, P3: 12）  
**代码质量**: 7.25/10（良好）  
**建议**: 修复 P1 安全问题后可投入生产，持续改进 P2/P3 项