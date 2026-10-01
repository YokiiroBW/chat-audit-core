# QQ .bak 样本探测记录（2026-07-20）

样本：.tmp/qq-bak-samples/聊天测试.bak

## 结果

- 外层是 ZIP 容器，未压缩；
- 内部有 4 个 db/<32位十六进制名称> 文件；
- 每个内部文件大小分别约为 353 KiB、2.2 MiB、4.6 MiB、4.7 MiB；
- 每个内部文件开头均为 SQLite header 3\0；
- 0x20 位置均出现 MSG0；
- 0x2B 附近包含 128 字节十六进制字符串；
- 0x400 之后是高熵数据，看起来是加密数据库页；
- 用标准 SQLite 尝试原文件、去掉 16 字节、32 字节、1024 字节均返回 ile is not a database。

## 判断

这个 .bak 不是 TXT/HTML/JSON 导出，也不是可以直接用现有 Python SQLite reader 打开的普通 SQLite。
它更接近 QQ 官方“聊天记录备份/恢复”产生的数据库容器：

`	ext
ZIP
└── db/<hash>
    ├── QQ 自定义数据库头（约 1024 字节）
    └── 加密数据库页
`

当前样本没有发现明文消息、表结构或可直接使用的数据库 key。

## 对项目的影响

可以在 Collector 内集成：

1. ZIP 安全解包到受控 staging 目录；
2. 校验 SQLite header 3 / MSG0；
3. 对每个内部数据库建立 manifest；
4. 用兼容的 QQ cipher 参数和正确 key 解密；
5. 接入现有 QQNT schema/parser/import queue。

但在没有 key 或 QQ 官方恢复接口的情况下，无法诚实地完成消息导入。直接把这些文件交给标准 SQLite、当前 ReadOnlySQLiteDatabase 或仅仅剥离 1024 字节都不够。

## 下一步条件

需要二选一：

- 提供该 .bak 对应的数据库 key/恢复凭据；或
- 明确该文件是通过 QQ 哪个功能、哪个版本导出的，以便继续匹配对应的备份解密协议。

在此之前，产品可以先实现 .bak 容器探测和安全解包，但不能标记为“已成功导入聊天记录”。

## 4. 用户确认的来源

- QQ 版本：`9.9.31-49738 (64位)`
- QQ 菜单：`聊天记录管理` → `导出聊天记录到电脑`
- 该入口说明为：将电脑 QQ 的聊天记录导出为文件，并支持后续将聊天记录导入其他电脑。

这说明样本是 **QQNT 桌面端导出备份容器**，不是“手机 QQ 聊天记录备份与恢复”格式，也不是简单的文本导出。

## 5. 处理结论

实现应拆成两层：

```text
聊天测试.bak
  → ZIP 安全解包
  → 4 个 db/<hash> 文件
  → QQNT MSG0/自定义头适配
  → SQLCipher/QQNT key 解密
  → 现有 schema + Protobuf + import queue
```

用户确认的 QQ 版本可以作为第一版格式适配版本号，但仍不能绕过数据库 key。样本本身没有提供可直接用于 SQLCipher 的明文 key。
