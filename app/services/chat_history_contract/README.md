# Chat 原服务历史查询与群互动候选

本批仅本地实现，schema 绑定固定 /internal/evidence/search、read-locator、check、group-stats。旧 /internal/evidence/read 保持原 wire。独立 service token 与服务器 ChannelMapping 决定账号、平台、源、群私/room；浏览器或模型不能选择 robot/服务URL/actor。search 以 timestamp/source_sequence/msg_hash 稳定keyset，cursor绑定完整范围、reader及filters/limit；它不冻结源数据。

归档状态与 Core 当前性分开。archive_current 只表示本次同映射/locator/hash 的实际归档仍一致，core_source_state 永远明确 not_observed。消费者还须核当前角色/群权限与 Memory 已知精确 alias suppression。没有 Core 对应的真实导入历史仍可查询，不伪造 Core receipt。

read-locator 依据实际 msg_hash，不以文本/昵称/数组位置猜ID。native_aliases 来自同已授权实际账号/导入源；external、qqnt_source、qqnt_platform 分开，原完整 aliases 超预算拒绝，不截漏。搜索按完整原文条目缩页，未返回项不跳过；单条过大明确413。所有正文无缓存。

group-stats 只表示窗口内实际原始 reply/@，同消息同目标 interaction只计1，@all单列，歧义 unresolved，不生成好感或“关系较好”事实。UTC桶边界与显式固定时区偏移一致；有界重算包含晚导入/删除。coverage.complete=false 时不能把扫描样本当全群总数；Memory 整理话题覆盖另列，不混为热度。证据locator每边最多3条，超总预算413，不裁边冒完整。

限制：search/check每次≤100，单条aliases≤256，统计≤10000消息，响应≤131072字节。当前权限和删除状态在实际消费前再次检查，不把check当长期授权。
