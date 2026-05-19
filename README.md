# astrbot_plugin_harassment_reporter

给 LLM 提供一个名为 `report_harassment` 的工具。

当模型在和用户聊天时，觉得自己正在被骚扰、辱骂、挑衅、恶意消耗，或者持续被不舒服地对待时，它可以主动调用这个工具，把情况上报到你预先配置好的会话里，这样你就能更快知道有人在折腾 Bot。

## 功能

- 提供全局 LLM 工具 `report_harassment`
- 允许模型主动上报骚扰情况
- 支持把上报发送到指定会话 ID
- 支持同一来源会话冷却，避免疯狂刷屏
- 支持自动附带最近几轮聊天摘要
- 支持上报成功后自动把对方加入观察名单
- 支持在 LLM 请求前注入提示，提醒模型在合适时机使用该工具
- 提供管理命令，方便绑定目标会话和测试链路

## 安装后先做什么

1. 把插件放进 AstrBot 插件目录并启用。
2. 找到你想接收报警的那个会话。
3. 在那个会话里执行：

```text
/harassment_bind_here
```

这样当前会话就会被保存为接收上报的目标会话。

如果你想手动填写，也可以先执行：

```text
/harassment_sid
```

拿到当前会话 ID 后，填进插件配置项 `report_session_id`。

## 管理命令

- `/harassment_sid`
  查看当前会话 ID

- `/harassment_bind_here`
  把当前会话设置成接收骚扰上报的会话

- `/harassment_unbind`
  清空接收会话绑定

- `/harassment_status`
  查看当前插件配置状态

- `/harassment_test [备注]`
  发送一条测试上报，确认链路是否正常

- `/harassment_watchlist`
  查看观察名单

- `/harassment_watch_remove <平台:用户ID>`
  从观察名单移除一个人

- `/harassment_watch_clear`
  清空观察名单

## LLM 工具说明

工具名：

```text
report_harassment
```

参数：

- `reason`: 为什么模型觉得自己被骚扰了
- `severity`: `low` / `medium` / `high`
- `evidence`: 可选，补充证据或摘录
- `expected_help`: 可选，希望你怎么介入

## 建议

- 如果你希望模型更积极一点地求助，保持 `inject_usage_prompt = true`
- 如果你不想当前骚扰者看到“已上报”之类的回显，可以把 `echo_result_to_current_session` 关掉
- 如果你担心刷屏，可以把 `report_cooldown_seconds` 调大
- 如果你希望上报更有上下文，可以保持 `attach_recent_context_summary = true`
- 如果你只想提醒、不想长期记录，就把 `auto_add_sender_to_watchlist = false`

## 新配置项

- `attach_recent_context_summary`
  是否在上报里自动带上最近几轮聊天摘要

- `recent_context_summary_lines`
  摘要最多取多少行最近上下文

- `recent_context_summary_max_chars`
  摘要总长度上限

- `auto_add_sender_to_watchlist`
  上报成功后是否自动把对方加入观察名单

- `watchlist_max_entries`
  观察名单最大容量，超过后会优先保留最近被上报的人

## 依赖

无额外第三方依赖。
