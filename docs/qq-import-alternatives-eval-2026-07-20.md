# QQ 聊天记录导入方案评估（NapCat 与官方客户端共存问题）

日期：2026-07-20  
范围：`chat-audit-core` 的 QQ 接入路径（实时 OneBot + 本地 QQNT 导入）  
状态：评估结论，不包含实现代码变更

## 1. 问题定义

当前主线依赖 **NapCat / OneBot 11 反向 WebSocket** 做实时聊天备份。  
实际使用中，**NapCat 与官方 QQ 客户端难以在同一台 PC、同一主力账号上“一边正常聊天、一边稳定采集”**，导致：

- 日常沟通仍依赖官方 QQ 体验；
- 备份/审计又依赖 NapCat 登录态；
- 两者抢登录、抢数据目录或需要改写/劫持 QQ 进程时，PC 端体验明显变差。

评估目标：

1. 有没有“可装插件的 QQ 客户端”，能在**一个客户端**里同时满足日常使用 + 消息导出/推送；
2. 还有哪些可落地的替代架构；
3. 与本仓库现有能力、安全边界的匹配度。

## 2. 本仓库现状（已实现 / 已踩坑）

| 能力 | 状态 | 说明 |
|---|---|---|
| NapCat / OneBot 11 反向 WS | 已实现 | `ws://.../onebot/v11/ws?adapter_id=...` |
| 多适配器 / 多账号 | 已实现 | `adapter_id` 隔离 |
| QQNT 本地库 Collector + GUI | 框架已完成 | `dist/chat-audit-qq-collector-gui.exe` |
| 真实 `nt_msg.db` 直读 | **停在** `DB_VFS_UNSUPPORTED` | 头为 `SQLite header 3\0` + `QQ_NT DB` |
| 双来源去重 | 契约/代码已设计 | NapCat 与 QQNT 可并存 |
| 安全边界 | 已写死 | 不注入 QQ、不自动提 key、不改原库 |

相关文档：

- `docs/qqnt-direct-read-research-2026-07-20.md`
- `docs/real-qqnt-readonly-probe-2026-07-18.md`
- `CODEX_HANDOFF.md`

## 3. 为什么 NapCat 会和官方 QQ“打架”

本地 refs：`C:\Users\Administrator\Documents\Codex\refs\NapCatQQ`

NapCat 不是“旁路只读官方客户端消息流”的独立 IM，而是 **基于 NTQQ 的协议端**：

| 形态 | 本地证据 | 与官方客户端关系 |
|---|---|---|
| Shell + Loader | `napcat-shell-loader/launcher.bat` 用 `NapCatWinBootMain.exe` + `NapCatWinBootHook.dll` 启动系统安装的 `QQ.exe` | 实质是**启动/劫持官方 QQ 进程**加载 NapCat，而不是另开一个完全独立的聊天 UI |
| Framework | `napcat-framework` 通过 `NCoreInitFramework(session, loginService, ...)` 挂入 QQ wrapper | 仍依赖 QQNT 运行时与登录态 |
| 独立 Shell 进程管理 | `napcat-shell` 多进程 Worker / WebUI | 仍是 bot 协议侧，不是日常聊天客户端替代品 |

因此共存失败通常不是“端口冲突”这么简单，而是：

1. **同一账号多端登录策略**：PC 协议端与官方 PC 客户端常互顶；
2. **数据目录 / 版本补丁耦合**：Loader 会改写启动入口（如 `qqnt.json` 的 `main` 指向 `loadNapCat.js`）；
3. **产品目标不同**：NapCat 目标是 Bot/协议 API，不是“官方 UI + 插件市场”的完整日常客户端。

**结论：** 把 NapCat 当成“可插件化官方 QQ”不现实；它解决的是协议接入，不是 PC 日常客户端共存。

## 4. 方案地图

### A. 可插件 QQNT：LiteLoaderQQNT + 插件

本地 refs：`C:\Users\Administrator\Documents\Codex\refs\LiteLoaderQQNT`

| 项 | 评估 |
|---|---|
| 是什么 | 官方 QQNT 的插件加载器（主题 / 扩展 / framework 插件） |
| 能否“一个客户端日常聊 + 导出” | **理论上最接近**：用户继续用官方 UI，插件在同进程扩展能力 |
| 与 OneBot 的关系 | 历史上常见路径是 LiteLoader + OneBot 类插件；当前 LLOneBot 主线已演进为独立协议端（见方案 B） |
| 安装成本 | 高：需未公开 `dbghelp.dll` 或修补 `QQNT.dll`（README 明确） |
| 账号风险 | **高**：README 警告安全中心可能视作“非法外挂”，设备下线甚至封号 |
| 版本风险 | **高**：QQ 一升级就可能失效，需持续维护补丁 |
| 与本项目边界 | 与“Collector 不注入 QQ”一致仍可外部可选；但若平台**官方推荐/内置**此路径，会抬高合规与封号连带风险 |
| 对本项目接入 | 若插件能提供稳定 OneBot 11 / 等价 WS，后端几乎零改；难点在客户端侧稳定性与风控 |

**推荐级别：** 仅作“极客自用实验”，**不作为产品默认方案**。

### B. 独立协议端：LLOneBot / LuckyLilliaBot

本地 refs：`C:\Users\Administrator\Documents\Codex\refs\LLOneBot`

| 项 | 评估 |
|---|---|
| 是什么 | 独立 Bot 服务，暴露 OneBot 11 / Satori / Milky |
| 运行模式 | `Direct`：native sign + TCP + WebUI 扫码；`PMHQ`：连本地 PMHQ 端口 |
| 是否可插件官方 QQ | **否**（Direct 不依赖官方 UI；PMHQ 依赖另一套本地桥） |
| 与官方客户端共存 | Direct 仍是**另开登录态**；同账号双 PC 仍可能互顶，**不从根上消除 NapCat 同类问题** |
| 对本项目 | OneBot 11 兼容度高，可作为 **NapCat 的可替换适配器**，降低单供应商风险 |
| 价值 | 部署/维护路径不同、API 生态不同；**不是**“日常 QQ + 备份一体客户端” |

**推荐级别：** 作为 **OneBot 供应源备选** 有价值；不解决主力账号 PC 共存痛点。

### C. 纯协议实现族：Lagrange 等

| 项 | 评估 |
|---|---|
| 是什么 | 不启动官方 QQ，直接实现 QQ 协议 |
| 共存 | 与 B 同类：独立设备登录；主账号日常仍用官方客户端时，受多端策略约束 |
| 优点 | 可放 NAS/Docker/专用机，PC 日常客户端干净 |
| 缺点 | 签名/风控/协议变更；封号与功能完整度（媒体、合并转发等）不确定 |
| 对本项目 | 只要输出 OneBot 11 或可适配事件流，即可接入；需单独验收媒体/历史消息能力 |

**推荐级别：** 适合 **专用采集账号** 或 **专用机器**；不适合“主力号仅开官方客户端且同机实时全量”的理想态。

### D. 双账号 / 双机架构（工程上最稳）

| 模式 | 做法 | 覆盖范围 | 体验 |
|---|---|---|---|
| D1 专用 Bot 号 | 群内拉入 Bot；NapCat/LLOneBot 只登 Bot 号 | 群聊好；私聊几乎无 | PC 官方客户端完全自由 |
| D2 专用采集机 | 小主机/VM/NAS 跑 NapCat，主 PC 只开官方 QQ | 取决于是否允许同号多端 | 主 PC 体验好，运维成本中 |
| D3 主号仅历史导入 + 辅号实时 | 主号走 QQNT 导入；关注群用 Bot 实时 | 折中 | 推荐组合拳 |

**推荐级别：高。** 这是目前唯一不依赖破解/注入、且能明显改善“PC 不好用”的路径。

### E. QQNT 本地库只读导入（本仓库已在推进）

| 项 | 评估 |
|---|---|
| 目标 | 官方客户端正常使用；Collector 只读快照导入历史 |
| 优点 | 不抢登录、不改 QQ UI、符合本仓库边界 |
| 当前阻塞 | 需：副本剥 1024 字节头 + SQLCipher 特定 PRAGMA + **用户提供** 16 字节 passphrase |
| 不做的事 | 自动 debugger 提 key、注入 QQ、宣称已支持真实全量解析 |
| 实时性 | 差（定时/手动扫描）；适合补历史与离线资产 |
| 与 NapCat | 可双源：实时走协议端，历史走本地库 |

**推荐级别：高（主账号历史/离线主路径）。**  
对“NapCat 不能日常共存”的补位意义最大，但不能单独替代实时在线采集。

### F. 官方导出 / 第三方仅导出工具

| 项 | 评估 |
|---|---|
| 官方导出 | 字段/媒体/格式通常不完整，难支撑审计浏览 |
| `qq-chat-exporter` 等 | 多仍依赖 NapCat 登录态，**不解决共存** |
| 价值 | 一次性迁移补充，不是常开方案 |

**推荐级别：低（仅补洞）。**

## 5. 对照表（面向本项目决策）

评分：5 最好 / 1 最差。风险 5 = 风险最高。

| 方案 | 主号日常体验 | 实时性 | 历史完整度 | 账号风险 | 工程接入成本 | 与仓库边界 | 综合 |
|---|---|---|---|---|---|---|---|
| A LiteLoader 插件化官方 QQ | 4（同一 UI） | 4 | 3 | **5** | 3 | 2 | 不推荐默认 |
| B LLOneBot 替换 NapCat | 2（仍可能顶号） | 5 | 3 | 3 | 2 | 4 | 备选协议源 |
| C 纯协议 + 专用机 | 5（主 PC 干净） | 5 | 3 | 3 | 3 | 4 | 推荐给实时 |
| D 双账号（Bot 号） | 5 | 5（群） | 2（无私聊） | 2 | 1 | 5 | 群聊首选 |
| E QQNT 本地导入 | 5 | 2 | 4（打通后） | 1 | 3（还差解密） | 5 | 主号历史首选 |
| 维持单机 NapCat 主号 | 1 | 5 | 4 | 3 | 1 | 4 | 现状痛点 |

## 6. 推荐架构（务实）

```text
                 ┌──────────────────────────────┐
                 │  主 PC：官方 QQ（日常聊天）    │
                 │  + QQNT Collector（定时导入） │
                 └──────────────┬───────────────┘
                                │ 历史消息 / 媒体状态
                                ▼
                     ┌─────────────────────┐
                     │  chat-audit-core     │
                     │  双源去重 + 审计 UI  │
                     └──────────▲──────────┘
                                │ OneBot 11 实时
                 ┌──────────────┴───────────────┐
                 │ 专用机 / Docker / Bot 号       │
                 │ NapCat 或 LLOneBot             │
                 └──────────────────────────────┘
```

### 分层建议

1. **主账号日常**  
   只保留官方 QQ。不在同机强行挂 NapCat 主号。

2. **主账号数据**  
   继续打通 **E：QQNT 只读导入**（用户自备 key → 副本剥头 → SQLCipher）。  
   这是对“不能装 NapCat”最对齐的补齐路径。

3. **实时群聊 / 持续在线**  
   用 **D1 Bot 号** 或 **D2 专用机协议端**（NapCat 或 LLOneBot 均可）。  
   后端继续 OneBot，不绑死 NapCat 品牌。

4. **明确不作为产品默认**  
   LiteLoader / 注入式 Hook / 自动提数据库密钥。

## 7. 与“有没有可装插件的 QQ 客户端”的直接回答

| 问题 | 答案 |
|---|---|
| 有没有类似可装插件的 QQ？ | **有生态**：`LiteLoaderQQNT` 是当前最接近的“官方 QQNT 插件平台” |
| 能否靠它优雅解决共存？ | **技术上可能**（同一 QQ 进程挂 OneBot 插件），但 **风控/封号/补丁维护成本高**，不适合作为本审计平台默认依赖 |
| NapCat 是不是这类客户端？ | **不是**。NapCat 是基于 NTQQ 的协议端/加载框架，目标不是插件化日常客户端 |
| 还有没有更干净的办法？ | **有**：主号官方 QQ + 本地库导入；实时用 Bot 号或专用机协议端 |

## 8. 可立刻做 / 需容器 / 阻塞

| 类别 | 内容 |
|---|---|
| 可立刻做（产品/文档） | 写清部署拓扑：主号不推荐同机 NapCat；给出 Bot 号与专用机推荐图 |
| 可立刻做（工程） | OneBot 适配层保持协议中立，验收 LLOneBot 作为第二供应源 |
| 需继续推进 | QQNT：用户 key + SQLCipher + 剥头后的真实 20 条扫描 |
| 阻塞 | 无独立、合法、可分发的“官方 NT VFS”；无官方稳定插件 API |
| 不做 | 内置自动注入、自动提 key、默认引导 LiteLoader 过检测 |

## 9. 资料来源

| 来源 | 路径 / 链接 |
|---|---|
| 本仓库 README / Handoff | `README.md`, `CODEX_HANDOFF.md` |
| 本仓库 QQNT 调研 | `docs/qqnt-direct-read-research-2026-07-20.md` |
| NapCat 本地克隆 | `C:\Users\Administrator\Documents\Codex\refs\NapCatQQ` |
| LiteLoader 本地克隆 | `C:\Users\Administrator\Documents\Codex\refs\LiteLoaderQQNT` |
| LLOneBot 本地克隆 | `C:\Users\Administrator\Documents\Codex\refs\LLOneBot` |
| 上游入口（对照） | <https://github.com/NapNeko/NapCatQQ>、<https://github.com/LiteLoaderQQNT/LiteLoaderQQNT>、<https://luckylillia.com> |

## 10. 一句话结论

- **没有**“低风险、可默认推荐、又能装插件替代官方 QQ 且完美共存”的银弹。  
- **LiteLoader 系**是唯一接近“插件化官方客户端”的方向，但账号与维护风险过高。  
- 对本项目更合理的是 **双轨**：  
  - 主号：官方 QQ + QQNT 只读导入（历史）；  
  - 实时：Bot 号或专用机上的 NapCat/LLOneBot（OneBot）。  
- NapCat 共存问题应通过 **部署拓扑** 解决，而不是继续寻找另一个同构“主号同机协议端”。

## 11. 本轮工程推进（2026-07-20）

已增加 `collector/qqnt/capabilities.py` 运行时能力探测：

- 区分标准库 SQLite、可选 SQLCipher provider 与 NT VFS；
- 不打开、不修改 QQNT 数据库即可完成预检；
- 即使本机存在 `pysqlcipher3` / `sqlcipher3`，仍明确返回 `DB_VFS_UNSUPPORTED`，因为当前项目尚未提供 QQNT 1024 字节头适配与兼容 VFS；
- 新增测试防止把标准 SQLite 静默接受未知 `PRAGMA` 误判为 SQLCipher。

当前可实现性结论未改变：下一阶段需要独立、可重复测试的“副本剥头 + SQLCipher 参数 + schema 读取”实验；在该实验通过前，不应把真实 QQNT 导入标记为已支持。
