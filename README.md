# Token 饭碗（astrbot_plugin_token_meal）

> 把 bot 真实烧掉的 token 变成她的一日三餐：聊得多就吃饱，不聊就饿，饿了会撒娇讨饭，口粮见底会懂事。

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件。**不查余额、不调任何外部 API、不额外跑一次 LLM、不生图、不发主动消息、不注册 LLM 工具**——它只读 AstrBot 自己在 `data_v4.db` 里记的 `provider_stats`，所以「饭量」就是真实消耗，而且这个插件本身几乎不产生额外开销。

最初是为一只叫「大肥鱼」的 QQ AI 女儿写的，但所有角色称呼都可以改，插件本身不绑定任何人格。

---

## 它长什么样

每轮对话，在 bot 看到的用户消息后面会附一行状态（模型可见，用户不可见）：

```
[饭量] 54 分饱（正好）｜今天吃了 0.9 碗（约 44 万 token）｜上顿 22 分钟前｜口粮还剩 756 万
```

状态不同，括号里的引导也不同：

| 状态 | 引导 |
| --- | --- |
| 饱腹 < 35 | 「有点饿，想撒娇就提一句讨口饭」 |
| 饱腹 ≥ 90 | 「吃撑了，可以打个嗝嫌他话多」 |
| 今日口粮见底 | 「今天的额度吃完啦，懂点事，别缠着他烧 token」 |

口粮只是拟人提醒，**不会真的拦截对话**。对按量计费、或者用免费额度过日子的实例，相当于一个温柔的刹车。

命令：

- `饭量`（别名 `查饭` / `吃饱没`）→ 详细账本：今天几碗 / 多少 token、累计多少顿、口粮还剩多少、上一顿什么时候。
- `投喂 小鱼干`（别名 `喂饭` / `加餐`）→ 手动加餐，饱腹 +6，食物名会写进状态，下一轮她就知道刚被喂了什么。

只在配置的「主人」私聊里生效，群聊不注入、不给看账本。

---

## 为什么不用现成的「余额当饭吃」插件

社区里已有「饭碗」「大肥鱼的白饭」这类插件，思路很好，但它们查的是 `https://api.deepseek.com/user/balance`——**DeepSeek 专属接口**。如果你用的是免费模型、中转站、或者其他厂商，装上去只会一直报「查不了」；就算另配一个 DeepSeek key，那个余额也跟你实际聊天烧的 token 没有关系。

这个插件换了个数据源：AstrBot 自己就在记每一轮 `token_input_other` / `token_input_cached` / `token_output`，直接用它，零依赖。

---

## 安装

要求 **AstrBot v4.x**（在 v4.29.3 上开发与验证）。用到了内核的 `TextPart.mark_as_temp()`、`ProviderRequest.extra_user_content_parts`、`get_astrbot_data_path()` 和 `provider_stats` 表，v3 不适用。

1. 把整个目录放进 AstrBot 的插件目录：

   ```
   data/plugins/astrbot_plugin_token_meal/
   ├── main.py
   ├── metadata.yaml
   └── _conf_schema.json
   ```

2. 在 WebUI 重载插件（或重启 AstrBot）。插件会自动生成配置文件和 `state.json`。

3. **必须配置 `owner_ids`**，填你的账号 ID（QQ 号等 sender_id），留空则插件完全不注入。

> 手动下载：GitHub 仓库右上角 `Code → Download ZIP`，解压后把文件夹放进插件目录即可。

---

## 配置

在 WebUI 插件配置页，或直接编辑 `data/config/astrbot_plugin_token_meal_config.json`：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `owner_ids` | `[]` | **必填**。主人账号 ID 列表，只有这些人私聊才生效 |
| `tokens_per_bowl` | `500000` | 多少 token 算一碗 |
| `daily_budget` | `8000000` | 今日口粮，见底不再长饱；`0` = 不限 |
| `decay_per_hour` | `3.0` | 饱腹度每小时衰减 |
| `satiety_per_bowl` | `6.0` | 每碗涨多少饱腹度 |
| `feed_bowls` | `1.0` | 一次投喂相当于几碗 |
| `count_cached` | `false` | 是否把命中缓存的输入 token 也算进饭量 |
| `umo_contains` | `""` | 只统计 `umo` 含此串的记录，留空 = 全部（含后台任务） |
| `inject_always` | `true` | 每轮都附状态行 |
| `hungry_threshold` | `35` | 低于此值提示撒娇 |
| `full_threshold` | `90` | 高于此值提示吃撑 |

调参建议：`tokens_per_bowl` 和 `daily_budget` 按自己实例的日均消耗来定。可以先 `饭量` 看两天实际数据再调；上下文很长的实例一轮常有几万 token，一碗太小会「一顿吃撑」。

---

## 实现说明

**记账**：`provider_stats` 按 `id` 增量读取，状态存在 `data/plugin_data/astrbot_plugin_token_meal/state.json`。首次启动会先读到当前最大 `id` 并回填今日累计与累计顿数，不会把历史 token 一次性算成「暴饮暴食」。读数据库走只读连接 `mode=ro`，失败只打 warning，不影响聊天。

**衰减**：懒结算，用到状态时才按 `now - last_update` 补算，没有后台定时任务，也不会发主动消息。

**注入**：用内核的 `TextPart(...).mark_as_temp()` 放进 `extra_user_content_parts`。这是 part 级标记——状态块不落会话历史，真实用户消息照常保存；不进 system prompt，所以不破坏 prompt cache。也刻意用纯 `[饭量]` 开头而不是 XML 标签，避免模型把标签误读成「系统指令」后自己编一套解释。

**不冲突**：不写人格、不改记忆、不注册工具、不抢 `on_decorating_result`，可以和 LivingMemory / reply_merge / proactive_chat 等插件共存。

---

## 隐私

插件不上传任何数据，不读取聊天正文，只碰 `provider_stats` 里的统计数字。`owner_ids` 属于你自己的配置，保存在本机 `data/config/` 下，**不会被提交进仓库**（仓库里的默认值是空列表）。

---

## 发布到 AstrBot 插件市场

推到 GitHub 后，还可以按 [官方发布文档](https://docs.astrbot.app/dev/star/plugin-publish.html) 去 AstrBot Cloud 的插件发布页提交仓库地址（需注册 AstrBot Cloud 账号），系统会自动解析 `metadata.yaml`。几个注意点：

- 版本号用语义化版本（`1.0.0`），不要带 `v` 前缀。
- 仓库里不要提交 `.git` / `__pycache__` / 开发配置，本仓库已用 `.gitignore` 排除。
- 发布用的 zip 不得超过 16MB（本项目只有 6 个文本文件，远低于限制）。

---

## 许可证

[MIT](LICENSE) · 欢迎改成人设、换成奶茶/猫粮/电量，或者拿去改成完全不同的状态玩法。

感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 及插件生态里的「饭碗」「大肥鱼的白饭」提供的灵感。
