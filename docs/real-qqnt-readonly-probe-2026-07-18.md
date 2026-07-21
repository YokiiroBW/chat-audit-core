# QQNT 9.9.31 只读探测记录

探测日期：2026-07-18

## 环境

- QQ：9.9.31.49738
- 主消息库：`nt_db/nt_msg.db`
- 主库大小：约 410 MB
- QQ 运行期间存在活跃 WAL/SHM 文件族
- 全程只读取文件元信息及前 64 字节，没有修改、重命名或注入 QQ 进程

## 结果

真实主消息库不是标准 SQLite 头：

```text
offset 0x00: SQLite header 3\0
offset 0x20: QQ_NT DB
```

官方 `wrapper.node` 内可确认存在：

- SQLCipher；
- `sqlite3_key` / `sqlite3_key_v2`；
- `sqlite3_vfs_register`；
- NT 数据库扩展头检查；
- 内部 `pskey` 管理与校验。

这些能力不是独立可加载的 SQLite 扩展，并依赖 QQ 内部组件。系统 Python
SQLite 无法打开该数据库；仅提供普通 SQLCipher 密钥也不足以复现 NT VFS。

## 安全停止规则

当前版本返回：

```text
DB_VFS_UNSUPPORTED
```

在获得版本兼容、可合法独立调用的 NT VFS 或官方导出接口前：

- 不自动提取 `pskey`；
- 不注入 QQ 进程；
- 不修改数据库头；
- 不把未知格式误报为密钥错误；
- 不声称已支持真实 QQNT 消息解析。

因此本轮没有执行 20 条真实消息扫描。后续适配必须先解决只读 VFS 层，
再进行 schema 与 Protobuf 版本适配。
