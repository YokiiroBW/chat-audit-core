# QQNT 本地库直读调研（2026-07-20）

调研目的：评估在**不依赖用户手动导出**的前提下，是否能稳定、只读地打开
Windows 官方 QQNT 本地消息库（`nt_msg.db`），并把结果接到现有 Collector。

## 1. 上次进度是否还在

还在，且没有被覆盖：

| 项 | 状态 |
|---|---|
| 分支 | `codex/qqnt-import-q0-q10`，工作区干净 |
| 本机只读探测 | `docs/real-qqnt-readonly-probe-2026-07-18.md` |
| 导入契约 / 后端 / Collector 框架 | 已完成；真实库停在 `DB_VFS_UNSUPPORTED` |
| 真实探测配置残留 | `.tmp/real-qqnt-probe/`（Git 忽略） |
| 本轮公开调研结论 | 本文 |

上次中断点：已发现公开候选仓库 `QQBackup/ntqq_msg_db_util`、
`shuakami/qq-chat-exporter`，尚未形成项目决策文档。本轮补齐。

## 2. 本机事实（已验证）

QQ：`9.9.31.49738`

真实主库文件头：

```text
offset 0x00: SQLite header 3\0
offset 0x20: QQ_NT DB
```

注意：这不是标准 `SQLite format 3\0`。Collector 的
`detect_database_format()` 会把它标成 `qqnt_custom_vfs`，并返回：

```text
DB_VFS_UNSUPPORTED
```

官方 `wrapper.node` 内存在 SQLCipher、`sqlite3_key` / `sqlite3_key_v2`、
自定义 VFS 注册与内部密钥管理。系统 Python SQLite **无法**直接打开该文件；
仅提供普通 SQLCipher key 也**不能**在未剥头/未匹配 cipher 参数时打开。

## 3. 公开方案地图

### A. 离线剥头 + SQLCipher 解密（最接近“读本地库”）

代表性项目：

- [QQBackup/ntqq_msg_db_util](https://github.com/QQBackup/ntqq_msg_db_util)
  （GPL-3.0，2026-07 仍活跃）
- 教程站 [QQBackup/QQDecrypt](https://github.com/QQBackup/QQDecrypt)
- 原理参考 [Mythologyli/qq-nt-db](https://github.com/Mythologyli/qq-nt-db)
- 密钥脚本 [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key)

公开流程：

```text
原样复制 nt_msg.db（及需要时的 WAL/SHM）
        │
        ▼
剥离前 1024 字节 NT 自定义头
        │
        ▼
按固定 PRAGMA 顺序打开 SQLCipher
        │
        ▼
导出明文 SQLite / 再做 schema 解析
```

社区确认的 cipher 参数顺序（顺序错误会失败）：

```sql
PRAGMA cipher_page_size = 4096;   -- 必须在 key 之前
PRAGMA key = '<16-byte passphrase>';
PRAGMA kdf_iter = 4000;
PRAGMA cipher_hmac_algorithm = HMAC_SHA1;
PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;
```

`ntqq_msg_db_util` 还说明：

- 密钥是 **16 字节 ASCII**；
- 大库可用 rowid 分页导出，损坏页可缩小批次跳过；
- 剥头是对**副本**操作，不是改原库。

Windows 密钥获取（公开教程，2026-07-19 仍标注可用）：

- 文档：`QQDecrypt` → `docs/decrypt/extract/NTQQ (Windows).md`
- 已确认 QQ 版本示例：`9.9.32-51246`（接近本机 `9.9.31`）
- 默认脚本：`windows_ntqq_get_key.ps1`
- 机制：静态定位 `wrapper.node` 中 `nt_sqlite3_key_v2` 引用，再**用调试器附着 QQ 进程**动态取出 passphrase
- 备选：IDA attach / 手工调试

**结论 A：**  
不是“独立 NT VFS 运行时”，而是：

```text
自定义 1024 字节头 + 非默认参数的 SQLCipher
```

“能读”的前提几乎总是：**先有 passphrase**。

### B. NapCat / 登录协议导出（不读本地库）

代表性项目：

- [shuakami/qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter)
  （GPL-3.0，约 4.2k stars）

特点：

- 通过 NapCat / 扫码登录读取并导出聊天；
- 支持媒体、多格式、定时导出；
- 不解决“主力账号只开官方客户端、不另开登录”的目标；
- 与本仓库已有 NapCat 实时链路能力重叠。

**结论 B：** 成熟，但**不是本地库直读**，不满足本期无感导入的关键约束。

### C. 注入 / Hook / 插件路线

公开可见的 Hook、LiteLoader、进程注入等项目，通常依赖改客户端或注入。

与本项目明确禁止项冲突：

- 不注入 QQ 进程；
- 不改 QQ 安装包 / 数据库头；
- 不自动提取 `pskey` / 密钥。

**结论 C：** 调研可知其存在，但**不纳入实现路线**。

## 4. 与本仓库边界的对照

| 能力 | 社区离线解密 | 本仓库现状 / 边界 |
|---|---|---|
| 识别 `QQ_NT DB` 头 | 剥 1024 字节 | 已识别并停止为 `DB_VFS_UNSUPPORTED` |
| 只读复制 WAL/SHM 族 | 通常要求复制 db | Collector 已有 family snapshot |
| SQLCipher 打开 | 需要 sqlcipher3 + 正确 PRAGMA | 当前 Python sqlite3 无此能力 |
| 密钥来源 | 调试器附着 / 手动调试 | **禁止自动提取**；规格允许“用户配置密钥” |
| 自动提密钥 | PowerShell/IDA/Frida | **第一版禁止** |
| 真实 20 条扫描 | 解密后才可能 | 尚未开始 |
| 消息 Protobuf | 社区有片段，完整实现分散 | Collector parsers 已有模拟/基础框架 |

开发规格 `chat_audit_core_qqnt_local_db_import_dev_spec_v1.md` 写的是：

- 推荐“支持 NTQQ 头部和 SQLCipher 的只读 VFS”；
- **运行时只使用已经配置的密钥**；
- **自动提取密钥不属于第一版**；
- 密钥失效 → `needs_key`，不暴力破解、不改库。

这与社区路径的可对齐部分是：

```text
用户自行提供 passphrase
→ Collector 在快照副本上剥 1024 头
→ SQLCipher 只读打开
→ 现有 scan/upload 管线继续
```

不可对齐部分是：

```text
自动 debugger 附着 QQ 提密钥
→ 违反“不注入/不自动提密钥”边界
```

## 5. 关键纠正：不是“神秘 VFS 完全无解”

前期停止码 `DB_VFS_UNSUPPORTED` 仍然正确，因为：

1. 文件前缀不是标准 SQLite；
2. 内嵌 VFS/SQLCipher 不能当独立扩展随便加载；
3. 没有密钥时不能诚实声称可读。

但公开资料表明，**更准确的工程描述**应是：

```text
NT 自定义文件头（约 1024 字节）
+ SQLCipher（特定 page_size / kdf / hmac）
+ 16 字节 passphrase
```

而不是“完全未知、只能靠 QQ 私有 VFS 进程内打开”。

对 Collector 的含义：

- 继续把“未剥头、无兼容 runtime”标为 unsupported，是对的；
- 若要推进，应增加显式能力门：
  - `header_strip_supported`
  - `sqlcipher_runtime_available`
  - `database_key_configured`
- 三者都满足，才允许真实扫描；否则保持 `DB_VFS_UNSUPPORTED` / `needs_key`。

## 6. 可行性评级（面向本项目）

| 路线 | 无感程度 | 安全边界 | 技术成熟度 | 推荐 |
|---|---|---|---|---|
| 用户提供 key + 副本剥头 + SQLCipher 只读 | 中（首次需人工给 key） | 较可接受 | 高（社区已跑通） | **主候选** |
| 自动 debugger 提 key 后同上 | 高 | 低（进程调试） | 高 | 不做 |
| 独立可分发 NT VFS 库 | 高 | 高（若真合法独立） | **目前未发现** | 继续观察 |
| NapCat / QCE 登录导出 | 中 | 中 | 很高 | 备用账号可用；主力账号不满足约束 |
| 手动 JSON/mht 导入 | 低 | 高 | 已有 | 仅兜底 |

## 7. 若采用“用户提供 key”主候选，最小增量

不改安全边界的前提下，建议分三步：

1. **Runtime 探测**
   - 检测是否存在可用 SQLCipher 绑定（如 `sqlcipher3` 或自带 DLL）；
   - 无 runtime 时明确报 `DB_SCHEMA_UNSUPPORTED` / 缺少 SQLCipher，而不是笼统成功。
2. **快照副本剥头**
   - 只对 snapshot 副本 `seek(1024)`；
   - 永不写原 `nt_msg.db`；
   - 保留原头校验（`QQ_NT DB`）。
3. **密钥验证与状态**
   - 从 DPAPI 读取用户配置的 `qqnt_database_key`；
   - 验证失败 → source=`needs_key`；
   - 成功才进入现有 `scan` / 上传批次。

验证门槛（建议）：

- 在本机 `9.9.31` 上，用**用户手动取得**的 key（不由本项目自动提取），
  对副本完成：
  - 打开 `sqlite_master`；
  - 只读抽样 `c2c_msg_table` / `group_msg_table` 各 ≤20 行；
  - 不上传、不改原库。

只有这一关通过，才谈 schema 适配和正式导入。

## 8. 明确不做什么

- 不把 `windows_ntqq_get_key.ps1` 或同类调试器脚本打进 Collector 默认流程；
- 不在文档里引导用户去注入/破解他人账号；
- 不修改真实 QQ 数据目录中的 db/WAL/SHM；
- 不在未验证 SQLCipher 参数前宣称“已支持真实 QQNT 导入”；
- 不因为社区能剥头，就删除 `DB_VFS_UNSUPPORTED` 保护。

## 9. 建议决策

**现在还不能说“已经可以直接无感读库”。**

更准确的判断：

1. **技术上有公开可复现的离线解密路径**（剥 1024 头 + SQLCipher + 16 字节 key）；
2. **密钥获取公开方案依赖进程调试**，与当前安全边界冲突，故不能做成默认无感；
3. **若接受“用户自行提供 key，一次配置后续自动同步”**，则与现有 Collector 架构最契合，也最接近规格原文；
4. **真正的完全无感**（零人工、零调试、零第二登录）目前仍缺合法独立 runtime/官方接口。

### 推荐产品形态

```text
首次：
  用户在可信环境下自行取得 key（项目外）
  → 粘贴到托盘设置（DPAPI 保存）
  → Collector 验证副本可打开

之后：
  定时只读快照 + 剥头 + SQLCipher
  → 增量扫描 → 上传 NAS
  → Web 浏览
```

这比“假装已有独立 VFS”诚实，也比“集成 debugger 提权”安全。

## 10. 参考链接

- 本仓库探测：`docs/real-qqnt-readonly-probe-2026-07-18.md`
- 导入契约：`docs/qqnt-import-contract-v1.md`
- [QQBackup/ntqq_msg_db_util](https://github.com/QQBackup/ntqq_msg_db_util)
- [QQBackup/QQDecrypt](https://github.com/QQBackup/QQDecrypt)
- [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key)
- [Mythologyli/qq-nt-db](https://github.com/Mythologyli/qq-nt-db)
- [shuakami/qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter)

## 11. 下一步（待你拍板）

1. **接受“用户提供 key”路线**  
   → 实现 SQLCipher runtime 探测、副本剥头、key 验证门闩，再做 20 条只读抽样。
2. **坚持完全无感且禁止任何密钥人工环节**  
   → 继续等待独立 runtime/官方接口；当前只能用 NapCat 旁路账号或手动导出兜底。
3. **仅文档/产品说明**  
   → 在托盘中把 `DB_VFS_UNSUPPORTED` 文案改成更准确的
   “需要 SQLCipher 运行时 + 用户配置数据库密钥”，避免误解为永久无解。
