# 上游设计与本项目的适配

在本人单账号、已有 dot、私人 MCP 桥接的范围内，当前架构可继续维护，无需整体重构。
微信协议和会话生命周期按固定源码核验，通用运行机制交给社区组件。

## 参考版本

- [cc-connect](https://github.com/chenhg5/cc-connect/tree/dfad19415a38b00b2c5c288610784d1a7eef337f)：
  `dfad19415a38b00b2c5c288610784d1a7eef337f`。审查微信 Platform 的收发、媒体、
  typing、context、限流，以及 Engine / Session 的 pending、完成、取消和版本隔离。
- [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin/tree/24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c)：
  `24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c`。参考 iLink 类型、媒体加解密、引用解析与 Markdown 过滤；移植部分的许可见
  [NOTICE](../NOTICE.txt)。

审查包含上游相关测试源码。本地环境没有 Go，没有运行 cc-connect 的测试，也不宣称覆盖未来版本。
分轮复现、修改和验证过程见 [历史审查](history/UPSTREAM_AUDIT.md)。

## 保留的设计

| 对照点 | 本桥接的适配 |
| --- | --- |
| 微信 Platform 负责协议，Engine 负责执行 | 通道负责收发和持久状态；原 dot 负责执行，不复制 Agent Engine |
| Session generation 隔离旧执行者 | 持久处理组、版本和终态一起提交；旧进度不能完成新版任务 |
| 进度与 turn 完成分开 | `final:false` 保留处理，最终回复或显式完成才终结；API `accepted` 仅表示受理 |
| 每会话缓存 typing ticket，有限周期刷新 | 十分钟缓存、五秒刷新；有效票据不因单次失败失效，新轮次不继承旧退避 |
| 收信去重、完整内容解析 | 消息与 cursor 原子提交，去重墓碑保留；空文本占位继续查找媒体、ASR 和引用 |
| 发送与限流有明确错误语义 | `ret=-2` 与 HTTP 429 启用冷却；跨进程发送中断保留 `uncertain`，不自动重发 |
| 停机与失败分开 | 未完成通知 / 下载保留恢复；停止输入使用独立三秒总期限 |

## 明确的差异

本人选择新来信入库就尝试显示输入提示，等待 dot 时也显示。桥接为真正新增来信登记
五分钟内存等待，不提前读取或标记 active；读取后接续处理，完成 / 停止 / 到期取消。
候选最多 128 个，处理优先；重复和重启不恢复旧等待。有有效创建时间时按上游启动前
两秒规则过滤历史，未知时间用本地接收时刻，依赖主机校时。接口成功不保证手机端显示。

正常长轮询截止参照 CC Connect 继续下一轮。桥接只接受完整请求已写出后的响应读取超时；
DNS、连接、TLS、写入、已知拒绝和取消仍失败，cursor 不推进。入站、媒体完成和工具提交
通过 AnyIO 唤醒 worker，保留周期兜底、串行发送与限流，不新增任务队列。

事件 ACK 不等于 dot 开始或完成执行。桥接没有模型内部事件流，不能用等待年龄推断
执行失败或终止 dot。等待年龄只作本机诊断，不据此发送微信文字提醒；明确发送失败才可能发送一次关联提示。
文件 / 视频传输也不代表 dot 必然能理解内容，云端 sandbox 路径不是本机路径。

## 基础设施与维护边界

### 本人课表提醒的限定扩展

用户授权仅本人每天首课前 15 分钟的一份课表主动发送。参考上述固定版本 cc-connect
`platform/weixin/weixin.go` 的 `Send`、`ReconstructReplyCtx`，以及
`weixin_test.go` 的 `TestContextToken_PersistAndReload`、
`TestReconstructReplyCtx_MissingToken` 和 push / reply 限流测试：采用已有持久化本人
context，共用串行 sender，错误按固定类别处理，不把主动推送伪装为新来信。
cc-connect 的 cron 会进入 Engine / Session；本项目只生成固定课表文字，不能直接
引入其执行器。也不采用其发送重试策略：错过提醒即跳过，uncertain 保留人工核对。

课表来源是本机 `gdufe-campus-agent` 的既有 `/api/jwxt/data?type=schedule&refresh=true`
返回，参考 commit `8ff2c0feccb9cbb6df66a0cc874f5ed14d36f023` 的
`packages/client-core/src/schedule-config.ts`、`schedule-periods.ts` 和对应测试。
沿用广州作息、实际 section 优先、单节 / 连堂 / 离散节次及行标题回退规则。
Python 桥接不能直接加载其 TypeScript 模块；教务 Python 客户端则依赖校园项目的登录
会话、缓存和服务配置，并返回额外原始 HTML。因此仅适配已解析 weekly JSON 和纯节次
规则，不复制登录、验证码、抓取或缓存基础设施，不修改校园项目。

调度复用已有 AnyIO 任务组，持久化复用 SQLite 原子迁移和事务，网络复用现有
WeixinWorker。新增表只表达课表快照及本人每日发送状态，不引入新框架、外部队列或
独立发送进程。数据库版本 9 向前迁移；回退旧程序前使用既有私人备份恢复流程。
本地回归验证这些适配；实际手机端主动收信仍需按 [课表提醒](SCHEDULE.md)验收。

官方 MCP SDK、AnyIO、HTTPCore、jsonschema、Standard Webhooks、SQLite 和 systemd
分别承担协议、并发、网络、参数校验、签名、存储和服务管理。选型及自研例外见
[贡献指南](../CONTRIBUTING.md#技术栈与复用原则)，依赖由 `uv.lock` 固定。

数据库事务必须覆盖消息、处理轮次、回复和分段；拆开为各自提交的仓库会破坏恢复语义。
现阶段不为单账号场景增加数据库框架、外部队列或新的执行器。

开源前维护已将 Events 表纳入版本 8 原子迁移；运行中 claim 恢复不混入建表。
DNS / DoH 和代理校验收敛到现有网络层，各业务边界仍返回自己的固定错误类别。
工具定义与私有路径校验移为独立模块，去掉反向导入；Store mixin 明示共享宿主要求，
保留原连接、锁、事务和组合顺序。没有新增运行依赖。

SDK 的 stdin 隔离仍使用已核验的私有入口并固定 2.3 系列；升级必须重新验证 stdio、
EOF、帧上限、取消和脱敏。大范围 Store 拆分与 SDK 大版本升级留给明确需求驱动。
当前部署证据见 [历史记录](history/REAL_VALIDATION.md)，新部署按 [验收清单](ACCEPTANCE.md)验证。

2026-10-04 维护验证：Python 3.11 / 3.14 各 380 项测试通过、零跳过；完整发布检查、
固定旧结构与实际版本 7 的升级演练通过。新增回归覆盖事件表迁移失败回滚、订阅和
在途事件保留、导入隔离、跨 mixin 原子事务、DoH 取消和三种调用边界。文档本地链接检查通过。
