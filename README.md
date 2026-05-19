# astrbot_plugin_harassment_reporter

给 AstrBot 的 LLM 提供一个名为 `report_harassment` 的工具。

当模型在和用户聊天时，觉得自己正在被骚扰、辱骂、挑衅、恶意消耗，或者持续被不舒服地对待时，它可以主动调用这个工具，把情况上报到你预先配置好的会话里。这样你就能更快知道有人在折腾 Bot。

这版插件除了基础上报，还支持：

- 自动附带最近几轮聊天摘要
- 上报成功后自动把对方加入观察名单
- 让 LLM 知道会把情况报告给谁，例如“主人”“狐狸”
- 控制 LLM 调用工具后的表现：静默、只警告、先警告再上报、上报后静默、上报后自然告知
- 可选让警告语、告知已上报的话、甚至给主人发的上报文本，都尽量贴近当前人设

## 功能

- 提供全局 LLM 工具 `report_harassment`
- 允许模型主动上报骚扰情况
- 支持把上报发送到指定会话 ID
- 支持同一来源会话冷却，避免疯狂刷屏
- 支持自动附带最近几轮聊天摘要
- 支持上报成功后自动把对方加入观察名单
- 支持让 LLM 知道“会报告给谁”
- 支持静默执行，不向骚扰者暴露上报动作
- 支持先警告再上报，或上报后自然说明
- 支持可选的人设化主人告警文本

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

## 推荐默认配置

如果你希望“尽量不惊动骚扰者，但必要时让 Bot 自己求助”，推荐这样配：

- `tool_response_mode = silent`
- `attach_recent_context_summary = true`
- `auto_add_sender_to_watchlist = true`
- `report_receiver_name = 狐狸` 或你喜欢的称呼

如果你更喜欢“先警告一次，不收敛再上报”，推荐：

- `tool_response_mode = warn_once_then_report`
- `natural_language_warn_reply = true`
- `warn_once_memory_seconds = 1800`

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
  查看观察名单和最近统计信息
  管理员可以直接用；如果你不是平台管理员，也可以在已绑定的骚扰上报接收会话里使用。

- `/harassment_watch_remove <平台:用户ID>`
  从观察名单移除一个人
  管理员可以直接用；如果你不是平台管理员，也可以在已绑定的骚扰上报接收会话里使用。

- `/harassment_watch_clear`
  清空观察名单
  管理员可以直接用；如果你不是平台管理员，也可以在已绑定的骚扰上报接收会话里使用。

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

## 工具调用后的策略

`tool_response_mode` 决定 LLM 调用工具后，对当前骚扰者怎么表现：

- `silent`
  直接完成上报，但不要让对方知道已经上报。

- `warn_only`
  不真正上报，只让 LLM 先按当前人设自然警告对方。

- `warn_once_then_report`
  第一次调用时先警告；如果后面又触发一次工具，才真正上报。
  默认上报后保持静默；如果打开 `warn_once_inform_after_report`，则上报后也会自然告诉对方。
  如果 `natural_language_warn_reply = true`，第一次警告会按当前人设自然表达。
  如果同时打开 `warn_once_inform_after_report` 且 `natural_language_report_reply = true`，第二次真正上报后也会按当前人设自然告诉对方。

- `report_then_silent`
  先上报，再保持静默，不告诉对方。

- `report_then_inform`
  先上报，再让 LLM 按当前人设自然表达“我已经报告了”。

注意：

- 插件不会再主动往当前骚扰会话里发送“上报已发送给主人”这种机械提示。
- 对骚扰者说什么，改成由工具返回结果去引导 LLM 自己自然表达。
- `warn_only` 模式下不会真正向主人发送上报，只是警告。

## 关键配置项说明

- `report_receiver_name`
  告诉 LLM 它会把情况报告给谁。比如设成 `狐狸` 后，警告时它就可以自然说“你再这样我就告诉狐狸你骚扰我了”。

- `natural_language_warn_reply`
  开启后，警告不会是死板句子，而是要求 LLM 基于当前人设自然表达。

- `warn_message_template`
  警告的核心意思模板。即使开启自然语言，LLM 也会围绕这个意思自由发挥。

- `warn_once_inform_after_report`
  只在 `warn_once_then_report` 模式下生效。
  开启后，第二次触发并真正上报之后，LLM 还会按当前人设自然告诉对方“我已经报告了”。
  关闭时则是上报后继续静默。

- `natural_language_report_reply`
  在需要告诉对方“我已经报告了”的场景下，是否要求 LLM 基于当前人设自然表达。
  既影响 `report_then_inform`，也影响开启了 `warn_once_inform_after_report` 的 `warn_once_then_report`。

- `report_inform_template`
  告知已上报时的核心意思模板。
  既用于 `report_then_inform`，也用于开启了 `warn_once_inform_after_report` 的 `warn_once_then_report`。

- `owner_report_style`
  给接收报警的那个会话发什么风格的消息。
  `structured` 是清晰的结构化告警。
  `persona_natural` 会尝试按当前会话人设把这条告警改写成更像角色自己在求助。

- `natural_language_report_to_owner`
  只有在 `owner_report_style = persona_natural` 时生效。
  开启后，插件会额外调用当前会话所用的模型，把原本的结构化告警改写成更自然、更贴近当前人设的上报文本。
  如果改写失败，会自动回退到结构化告警，不影响主功能。

- `attach_recent_context_summary`
  是否在上报里自动带上最近几轮聊天摘要。

- `auto_add_sender_to_watchlist`
  上报成功后是否自动把对方加入观察名单。

- `watchlist_entries`
  配置页里的可编辑观察名单。
  你可以直接在这里查看、添加、删除或修改观察对象；命令 `/harassment_watchlist`、`/harassment_watch_remove`、`/harassment_watch_clear` 和自动加入观察名单也会共用这份数据。
  建议把 `key` 填成 `平台:用户ID`，例如 `default:2127074778`。

## 可用占位符

下面这些模板字段可以在 `warn_message_template` 和 `report_inform_template` 里使用：

- `{receiver_name}`
- `{reason}`
- `{severity}`
- `{severity_text}`
- `{sender_name}`
- `{sender_id}`
- `{message_text}`
- `{evidence}`
- `{expected_help}`

## 一些使用建议

- 如果你最在意“别让骚扰者知道”，就用 `silent` 或 `report_then_silent`。
- 如果你想让 Bot 有一点“自我保护”的戏剧感，可以用 `warn_once_then_report`。
- 如果你很喜欢当前人设的表现，想让给主人发的报警也更有角色味，可以打开 `owner_report_style = persona_natural` 和 `natural_language_report_to_owner = true`。
- 如果你只想提醒、不想长期记录，就把 `auto_add_sender_to_watchlist = false`。
- 如果你担心刷屏，可以把 `report_cooldown_seconds` 调大。

## 依赖

无额外第三方依赖。
