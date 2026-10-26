# TS-315 服务历史查询与群互动候选

本地候选，不是已部署服务或已完成 Companion/Memory/Platform 联合接入。复用 TS-061 的独立服务 Bearer、固定 channel→归档账号/来源登记及原消息库；不开放管理 API、头像补全、媒体下载或聊天写入。原 `/internal/evidence/read` 请求/响应保持兼容。

本实现消费根协调者冻结的 `chat-history/candidate-v1` 包，副本在 `app/services/chat_history_contract/`。manifest 的 UTF-8 LF SHA256 为 `4395f4207ef4bcaacb45522213aa622c5603d79006c29b48b5ff350525082ef4`。新 API 装配核对这个 pin 和完整包文件；缺失/变动返回 503，旧 read 不受新包故障影响。请求/响应及完整合成示例见该包。无新凭据配置，服务授权仍用原 `EVIDENCE_READ_CONFIG`；游标签名复用现 `APP_SECRET_KEY`，不复用管理员 token。

## 接口

| 路径（均 POST） | 作用 |
| --- | --- |
| `/internal/evidence/search` | 原文 literal 子串、真实作者 ID、时间闭区间过滤；稳定 keyset 分页 |
| `/internal/evidence/read-locator` | locator+预期原文 hash 精确读取，并取各侧最多十条邻读；不要求存在原生 external ID |
| `/internal/evidence/check` | 每批最多一百个 locator，在当前登记范围重新查询，返回 archive_current/changed/missing |
| `/internal/evidence/group-stats` | 同账号/精确来源/群的有界互动与热度观察；不接受私聊映射 |

channel 只能命中服务器原登记，不接收任意 robot/account/source/actor/URL。账号可见性保留 realtime RobotMessage 与 imported-source EXISTS 两条路径；指定 QQNT source 的登记还把整个选集、邻读和关系候选限定到该 source，而不仅限制中心消息的 alias。群私、平台、room 始终同时限定。

搜索按 `(timestamp, coalesce(source_sequence,-1), msg_hash)` 降序，cursor 严格查下一段更旧记录；同秒、空序号不漏页。cursor 绑定当前实例/完整 mapping、reader、全部过滤和页大小，并由现服务端 secret 派生密钥签名；修改条件或轮换 token/secret 后需从头查询。每页上限一百条，完整文本和总响应沿原大小限制；总字节超额可减少完整条目数，但 cursor 不越过未返回记录。不能完整、安全提供的原文（超大、NUL 截断、结构化/嵌套 CQ 等）仍明确 413/422，不伪装为空文本或成功截断。keyword 只查原始文本，不把昵称当人物授权。

新 record 保留原作者、时间、存储 hash、稳定 locator、归档观察 ID，增加当前注册命名空间的唯一 `message_id`（无/歧义时 null）及实际 native_aliases。aliases 明分 external/qqnt_source/qqnt_platform、账号、群私、room、import_source_id；只取实际同账号/来源的行。完整集合超过 256 时失败，不切掉可能关联显式遗忘的 alias。

check/read-locator 的 archive_current 只证明 Chat 当前登记下的存储行与给定 hash 一致；不是 Core receipt、修订/撤回或 Memory suppression 证明。改为超过 SQL 文本投影大小的原文时，changed 的当前 hash 为 null，不计算截断前缀的假 hash。旧 locator 换绑范围后为 missing。没有 external/Core 对应的合法导入历史仍可查，不能因此冒造 receipt。

消费者实际使用前，仍须核当前 Platform audience/scope、Chat locator check，再按已知同物理源 aliases 过滤 Memory 显式遗忘。此联合接点尚未验收，不能把本卡 HTTP200 当作上述约束已经完成。服务 token 不交给模型或浏览器。

## 互动与热度

统计只针对 Chat 原始消息，不包含 Memory 话题整理覆盖、好感、情绪或共同同意推断。程序读取必要 MessagePart reply/@，旧 QQNT reply 解码复用已有纯解析器；无 typed parts 时才解释可安全读取的 CQ 关系。原始 typed text 中出现 CQ 字面串不当成真实 @。不展开转发、嵌套结构化内容或附件。

reply 按同账号/来源/群私/room 查询全部候选，ID 重用时 unresolved，不使用管理预览的 latest winner。@ 必须有明确 ID，且在同可见范围有真实作者对应；不凭昵称、他群或其他账号 alias 合人。一个物理消息到一个目标的 reply+@ 只计一次 interaction，reply/mention 分列；重复 @ 不双计，@all 单列广播。每边最多三个真实证据 locator；图边不写回人物关系账本。

每次扫描最多一万条，按时间升序取有界集合。coverage 标明扫描数、has_more、complete、未解析/不可用关系；超界显示不完整，不能把 prefix 样本当全群总数。响应超过总字节预算直接 413，不偷偷删边。桶使用显式固定时区偏移及小时/日粒度，不宣称处理了窗口内 DST 变更。BOT 流式分段暂按原生物理消息计数：现档案没有共同 Core reply/turn ID，不能按时间或文本猜合并。

所有查询是 live observation/keyset，不是跨请求快照。晚导入、删除后重新查询选定窗口会重新计算，不保存 sent_at 水位。并发变化可能使不同请求的统计有差异；需要查证时用 locator check，而非把图形布局/聚合数当人物事实。

## 验证和后续

本卡相关实际测试 `78 passed, 1 skipped`；包括生产路由、真实 SQLAlchemy/合成 SQLite、原 loopback HTTP 回归。唯一 skip 为原 Windows `fcntl` 完整工厂限制；没有给备份锁放空替身。未运行真实 PG、Linux 完整工厂、真实聊天库、QQ、外部模型或部署。本地交接与原始命令见 `docs/handoffs/TS-315.md`。后续由协调者串行组织 Linux/真实 PG 和消费者联合，不重复未变化的旧检查。
