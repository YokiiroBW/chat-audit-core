# DQ03 消息身份与归档去重

本地开发完成，尚未集成、推送、部署或进行真实使用验收。

基线：`bb8937a7a712538d55af3c2af0b3cc47a917038b`，隔离分支 `codex/quality-life-20261003`。实机 `4d750ad` 的相关消息服务文件与本基线为同一 blob；这里只对该路径作修复，不把整个源码检出等同实机版本。

导入和实时采集共同使用 `MessageService.resolve_existing_message`：优先匹配既有来源行身份，再匹配会话范围内的消息 ID 及 `MessageSourceRecord` 平台别名。删除两处正文、秒级时间的猜测合并。无可靠 ID 的实时到达保存为独立消息，避免静默丢弃；无法证明其重试身份时也不会声称幂等。继续复用现有消息池、来源映射、媒体升级和机器人关联，不引入存储或 schema 迁移。

验证：依项目 `requirements-dev.txt` 在忽略的 `.runtime/dq-tests` 安装既有测试依赖；`python -m pytest tests/test_message_identity.py -q --basetemp .runtime/tests-dq03` **10 passed**。覆盖同秒同文不同 ID、两个导入来源、同 ID 重投、平台别名、来源行重放时导入 ID 变化、导入/实时顺序交换、群/私聊隔离及无 ID 的独立到达。`python -m compileall -q app tests` 与 `git diff --check` 通过。此固定源码快照原先未包含 tests 目录，本次增加了沿用其 pytest 配置的必要回归测试，未将未知旧测试结果记为通过。

限制：没有读取真实聊天或生产数据库；不能据本地复现认定线上已误合并。已丢失的历史原文是否可恢复需要原始采集证据，未自动重建或修改真实历史。未执行真实 QQ、NAS、推送或部署。协调者可依据该提交审查后串行集成，再安排独立生产验收。
