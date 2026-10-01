# TS-061：归档证据只读桥接（产品内候选）

2026-09-14。主协调任务已批准本地候选实现；**没有发布 I06 跨产品 wire，没有修改 text-dialogue/v1 1.0.0**。消费者适配与共同验收仍待协调者组织。本接口不能据此宣称某个 Core revision 已归档或当前来源可用于记忆。

## 原有标识与复用结论

| 现有对象 | 含义及限制 |
| --- | --- |
| `Adapter.id/current_robot_id` | 连接入口及此刻登录账号；换号会改变后者，不作为稳定档案授权 |
| `BotProfile.id`、`RobotMessage.robot_id` | 归档账号与账号可见关联；昵称不参与权限 |
| `Message.msg_hash` | 全局消息记录键；多账号可关联同一条，不代表谁都能读 |
| `Message.platform/message_type/room_id` | 平台、群私、原生会话；三者一起限定，不能凭 room_id 推断群私 |
| `Message.external_message_id` | 原生入站 ID；OneBot 不同账号可能重号，查询范围内也可能重用 |
| `ImportSource.id/account_id/device_id` | QQNT 导入来源、账号与设备 |
| `MessageSourceRecord.source_external_message_id/platform_message_id` | 两种导入 ID，不自动视作 OneBot ID；候选映射显式选种类和来源 |
| `Message.created_at` | 存储创建时间，不是 Core 收件 receipt 或当前修订 |

既有 `GET /api/messages`、`GET /api/search` 为管理员控制台查询，可补头像、回复预览、媒体和导入详情。不直接转发给陪伴或记忆。新查询只复用 `app.message_scope.apply_robot_message_scope`，同时应用其 join 与 where：账号实时关联或该账号 QQNT 导入关联，再额外限定平台、群私、会话。QQNT 别名还限定 `ImportSource.id/account_id/platform`，不从其他账号的别名反查本账号可见记录。

桥接不改采集、幂等归档、消息存储语义、迁移或备份；没有新数据库表、管理角色系统或归档写端点。已有档案如发生上游错误合并，桥接不能还原丢失的信息；同一范围 ID 指向多行时返回 404，除非正确 locator 在同一候选集合中消歧。

## 服务登记与授权

`EVIDENCE_READ_CONFIG` 是 JSON 字符串，默认空，空或无效时接口返回 503。下面值全为合成示例；生产不要使用示例 token/hash。

```json
{
  "mappings": [{
    "mapping_id": "group-a",
    "channel": {
      "namespace": "qq", "binding_id": "binding-a",
      "channel_conversation_id": "group:42", "thread_id": null
    },
    "robot_id": "account-a", "room_id": "42", "message_type": "group",
    "id_kind": "external", "import_source_id": null
  }],
  "readers": [{
    "service_id": "memory-evidence-reader",
    "token_sha256": "REPLACE_WITH_SHA256_OF_A_NEW_RANDOM_SERVICE_TOKEN",
    "mapping_ids": ["group-a"]
  }]
}
```

hash 必须是 64 位小写十六进制，示例占位符会被拒绝。为每个可信后端服务产生独立高熵 token，配置其 UTF-8 SHA-256，服务调用用 `Authorization: Bearer ...`。不得复用管理员或 OneBot token。只登记具体 channel，不支持通配符、昵称、人名或任意 actor/person 授权。私聊与群聊各自映射，原生相同 room ID 也不能合并；仅支持 QQ 与 `thread_id=null`。`qqnt_source/qqnt_platform` 必须提供具体 `import_source_id`，`external` 必须不提供来源 ID。

重复 token hash、reader ID、mapping ID、channel，以及不存在的 mapping 引用均使整份配置无效。不会取第一份配置继续运行或回退管理员权限；只接收 Bearer，不接受 cookie、`x-admin-token` 或 URL token。配置随服务进程 Settings 加载，环境变量修改后需按已有部署流程重启；每次请求重新验证当前 Settings，不缓存授权成功或旧 locator 权限。更换账号映射、服务权限或 token 后，旧 locator 不赋予额外权限。

本接口授权对象是可信后端服务及其登记读取范围。前端、用户或模型不能自行持有该服务 token；后端必须先根据可信渠道来源及当前权限选择请求。这个静态范围不是最终用户实时权限服务，也不能判断 Core 撤回或 Memory 遗忘。

## 请求和响应

端点为 `POST /internal/evidence/read`。实际产品内 OpenAPI 和合成请求/响应见 [候选 schema](contracts/evidence-read.candidate.openapi.json)、[实际合成样例](contracts/evidence-read.candidate.example.json)。这些文件不是根工作区共享合同。

```json
{
  "channel": {
    "namespace": "qq", "binding_id": "binding-a",
    "channel_conversation_id": "group:42", "thread_id": null
  },
  "message_id": "7", "before": 0, "after": 0
}
```

可加上先前响应的 `locator`，但仍必须提交相同的可信 channel 和该映射 id_kind 对应的 message_id。请求不接受 `receipt_id`、`revision`、`person_id` 或 `actor_id`，不会把它们拿来查询 Audit 内部主键。首次取到目标后，重复相同请求所得观察回执稳定，不增加归档记录。

成功响应包含：

- `status=archive_observed`、`current_source_state=not_checked`：仅证明当前查询看到了该档案。
- `target.text`：存储文字；`text_representation` 区分完整存储文字和非文字片段占位。`stored_content_sha256` 是完整已存 `raw_message` UTF-8 摘要，含 CQ 原始标记；占位输出不能用来冒充该摘要的原文。
- `target.locator`：产品内稳定定位值，含实例、具体 channel/账号/群私/room/id_kind/source 映射摘要、消息键和存储内容摘要。内容或映射改变时旧值失效。
- `archive_observation_id`：对 locator 和作者/时间/顺序/原生 ID 生成的确定性摘要，没有查询时间。它是 Audit 对当前存储行的观察回执，不是独立持久收件队列或已验证 Core revision 的签名证明。
- 作者原生 ID、时间戳、顺序和原生 external_message_id 是显示/核对元数据，不能反向授予权限。QQNT-only 记录的 external_message_id 可为 null，不能将其与请求别名混淆。
- `context.before/after`：每侧 0–10 条，默认零；所有邻居重新应用相同账号/平台/群私/room 过滤，按 `(timestamp, coalesce(source_sequence,-1), msg_hash)` 稳定排序。`complete=false` 始终明确它不是完整会话，也不是跨请求一致性快照；缺档、历史导入和并发采集都可能改变后续窗口。

只返回已保存文本与最小元数据，不加载 `local_message`、头像、媒体记录、来源 raw JSON、NAS 文件或其他消息。普通 CQ 非文字段替换为占位；JSON/XML/合并转发/嵌套节点及无法安全提取的内容整体不可用，不遍历它们。以对象/数组开头的结构化或疑似结构化原文也不可用（包括用户手写 JSON，属当前候选限制）。文字里用户自己输入的普通链接仍属于文字；接口不提供由媒体字段派生的附件 URL。所有返回均应作为不可信聊天内容处理，不能当作模型指令。

单记录限制为 16,384 字符及 32,768 UTF-8 字节，总 JSON 响应不超过 131,072 字节。SQL 已限制每行载入的原文前缀，并检查完整长度；任何一条或总响应过大都整体拒绝，不静默截断原句、不跳过过大的邻居继续声称完整上下文。SQLite 空字符导致的前缀读取也会拒绝。`media_state=unavailable`，不宣称文件可读、已备份或 TG 已支持。

| HTTP | detail / 含义 |
| --- | --- |
| 200 | 真实档案观察及有限文本 |
| 401 | `invalid_evidence_credentials` |
| 404 | `evidence_not_found`：无权、未知映射、不存在、原生 ID 歧义、冲突或过期 locator 统一结果 |
| 413 | `evidence_too_large`：无法在明确上限内完整提供 |
| 422 | 请求字段错误，或 `evidence_text_unavailable` |
| 503 | `evidence_unavailable`：未配置或配置冲突 |

候选成功和业务错误带 `Cache-Control: no-store`。配置验证不输出配置值，路由不记录 token、原文或来源路径。对未授权调用先验证服务与范围，私有记录是否存在不影响 404 内容；不承诺抵抗所有统计时序侧信道。

## 最小消费者调用与权威边界

以下是接入形状，需由消费者把 `channel/message_id` 从可信来源与登记映射取得；`base_url`、token 均无生产默认值。

```python
import httpx

def read_archive(base_url, token, channel, message_id, locator=None):
    payload = {"channel": channel, "message_id": message_id, "before": 0, "after": 0}
    if locator is not None:
        payload["locator"] = locator
    response = httpx.post(
        base_url.rstrip("/") + "/internal/evidence/read",
        headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10,
    )
    response.raise_for_status()
    return response.json()
```

Chat Audit owns 原始档案与真实归档观察。Core owns 当前消息修订、撤回、短期收件及 `receipt_id`；Memory owns 整理记录、投影、遗忘和当前记忆权限。读取成功后消费者仍必须独立核对 Core 当前版本/内容与 Memory 当前范围/遗忘状态。**不能单凭本响应把 common.source.archive_state 改为 archived 并宣称当前 Core revision 有效**；原生 ID 可能复用、旧内容可能保留，跨产品 receipt/revision 对应尚无冻结 wire。

群共享兴趣必须由 Memory 产生独立 `shareable_projection`。不要把本接口可读的私聊原文、locator 或血缘直接塞进群模型。上下文邻居也各自没有 Core 当前来源核验，不因中心消息有效就全量进入记忆。

## 验证与已知接点

新增测试：`python -m pytest tests/test_evidence_service.py tests/test_evidence_api.py -q`。测试全部使用内存/临时 SQLite、合成消息和独立测试 token；真实 HTTP 用 `127.0.0.1` 随机端口，结束关闭服务器、连接和数据库。没有真实上游权限或渠道服务参与。

Windows 当前不能导入完整 `app.main`：既有 `app.backup.jobs` 顶层依赖 Unix `fcntl`。候选 HTTP 验证使用最小 FastAPI 宿主挂载实际产品路由，真实跑认证、SQL、响应序列化和网络；不是完整应用启动、备份锁、Linux、PG 或真实 QQ/NAS 验证，也没有给 `fcntl` 填空实现伪造通过。主应用接线需在支持的 Linux 环境进一步验证。实际命令及结果见 [TS-061 交接](handoffs/TS-061.md)。
