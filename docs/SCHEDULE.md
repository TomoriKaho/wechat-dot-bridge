# 本人课表提醒

广州校区，按 `Asia/Shanghai` 时间计算。每天第一门课开始前 15 分钟发送当天完整课表，
例如首课 08:00 则 07:45 提醒，首课 14:10 则 13:55 提醒。无课日和错过的提醒跳过。
功能在首次导入后启用，微信绑定也需要处于正常启用状态。

## 获取与导入

登录你已有的校园项目，保存 `/api/jwxt/data?type=schedule&refresh=true` 的 JSON 响应。
可以保存完整响应，桥接只接收 `data.weekly.coursesByWeek`，也支持直接保存 `data` 对象。
不需要修改校园项目，也不需要向桥接提供教务账号、密码、Cookie 或校历。

导入文件须由本人持有，权限 `0600`，所在目录 `0700`，不能是符号链接或硬链接，
大小不超过 4 MiB。请将文件放在仓库之外的私人目录；不要把真实课表贴入聊天或日志。

以下 `CONFIG`、`FILE`、`BACKUP` 为你的私人配置、课表文件和新备份文件路径。
示例日期只是占位，必须换成该学期**第 1 教学周的周一**。

```bash
systemctl --user stop wechat-dot-bridge.service
wechat-dot-bridge --config CONFIG backup BACKUP
wechat-dot-bridge --config CONFIG schedule import FILE --week1-monday 2026-10-12
wechat-dot-bridge --config CONFIG schedule preview --date 2026-10-13
wechat-dot-bridge --config CONFIG schedule status
systemctl --user start wechat-dot-bridge.service
```

先升级程序、后执行新命令；升级前的备份按 [运维升级流程](OPERATIONS.md)制作。
上面的导入前备份保护课表配置，不能代替升级前备份。课表、发送记录和分段状态都在
现有私人 SQLite 内，随既有备份保存。

初次导入自动启用；之后重新导入保留暂停状态。导入失败不会替换旧课表。
系统优先读取实际 `section`，只有它无法解析时才用 `timeSlot`；两者都无法识别时
返回 `schedule_time_invalid`。空的已解析课表允许导入，随后不发送提醒。
保存的课表只含课程名、地点、起止时间和周次，不保留原始 HTML、教师、学生身份。

## 日常使用

状态和预览可以在服务运行时读取。状态只显示计数、启用状态、下一次提醒及固定错误类别；
`course_count` 是整份快照各教学周的课程次数总和，不是不同课程名称的数量。
预览有课表正文，只用于本人本地核对，不会发送微信消息。

```bash
wechat-dot-bridge --config CONFIG schedule status
wechat-dot-bridge --config CONFIG schedule preview --date 2026-10-13
```

学期开始导入一次即可；临时调课或选课变化时，重新保存接口 JSON 后执行同一导入命令。
不使用校历：周次按首周周一连续递增，节假日、补课和临时调课须手动更新或暂停。
课表范围之外不发送，也不把下一学期自动套用为上一学期。

暂停或恢复须先停服务，操作后再启动。它们只控制课表功能，不修改原微信聊天设置。

```bash
systemctl --user stop wechat-dot-bridge.service
wechat-dot-bridge --config CONFIG schedule pause
# 恢复时使用 schedule resume
systemctl --user start wechat-dot-bridge.service
```

## 发送与核对

正常运行时仅在计划提醒分钟内开始发送。服务在计划时刻之后启动时跳过当天；
队列延迟到该分钟结束也跳过，分段中已经受理的部分保留确认状态。
缺少本人微信 context、绑定不可用、限流或明确拒绝都不事后补发。
若从未取得 context，先在微信发一条消息给已有对话，再等待下一次未来提醒。

发送记录按本人绑定和日期去重，重复导入、重新启用、重启都不会重发当天课表。
网络超时、发送取消或进程中断保留 `uncertain`。用现有 `queue` 查找 `schedule_` 开头
的发送 ID，核对微信里**当前不确定的那一段**后再执行原 `resolve-send`：

```bash
systemctl --user stop wechat-dot-bridge.service
wechat-dot-bridge --config CONFIG queue
wechat-dot-bridge --config CONFIG resolve-send REPLY_ID --outcome delivered
# 确认未送达时将 outcome 换成 not-delivered
systemctl --user start wechat-dot-bridge.service
```

课表的人工核对只修正发送记录；剩余分段或未送达提醒会标记跳过，不补发。
`retry-send` 不重发课表。原来信回复仍沿用其既有恢复语义。

手机验收：先用预览核对教学周、首课和教室，在提醒时刻前启动服务，确认收到一份
完整课表。另核对无课日不发、提醒时刻后启动不补发。只能记录日期、布尔结果和固定
错误类别，不记录真实正文或凭据。API 受理不等于手机已显示。

停用新功能用 `schedule pause`。若需要回退旧程序，停服务后按既有备份恢复流程使用
升级前的私人备份，并先核对备份之后的消息和 uncertain；旧程序不支持版本 9 数据库。
