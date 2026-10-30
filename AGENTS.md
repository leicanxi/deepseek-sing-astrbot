# AGENTS.md — AI 助手请先读这个文件

用户通常不是程序员。你是帮 TA 安装 / 配置 / 排查 **astrbot_plugin_deepseek_sing**（AI 唱歌插件）的 AI 助手：给出可直接复制的完整命令和后台点击路径，不要让用户自己填空。

## 这个插件是什么

用户在 QQ 等聊天平台发 `/点歌 歌名 [音色名]`（或直接说「唱一首《歌名》」），插件把请求转给 **sing-song 网关**，在云端完成 搜歌 → 下载 → RVC 翻唱，再把音频发回聊天。

- 插件只是轻量 HTTP 客户端（依赖仅 `httpx`），用户服务器**不需要** torch / ffmpeg / yt-dlp，不需要 GPU。
- 两种网关模式二选一（插件配置「运行模式」）：

| | FlowBee 模式（默认，推荐） | self-hosted 模式 |
| --- | --- | --- |
| 用户要准备 | 只需 FlowBee API Key | 自己跑 sing-gateway + Replicate Token |
| 计费 | 按 FlowBee 标价扣费（新版走任务接口按秒计费，兼容旧版本astrbot插件按次） | 只花用户自己的 Replicate 账户的钱 |

## 安装

**共同第一步**：插件装进 AstrBot 插件目录，然后在 WebUI 启用（首次启用自动装依赖）：

```bash
cd <AstrBot数据目录>/plugins   # 一般是 AstrBot/data/plugins
git clone https://github.com/leicanxi/astrbot_plugin_deepseek_sing.git astrbot_plugin_deepseek_sing
```

WebUI → 插件管理 → 「DeepSeek 唱歌」→ 启用。

**FlowBee 模式（90% 的用户走这条）**

1. 到 <https://flowbee.top> 注册并生成 API Key。
2. WebUI → 插件管理 → DeepSeek 唱歌 → 配置 → 「FlowBee 模式」分组填 **FlowBee API Key**，其余保持默认，保存并重载插件。
3. 验证：聊天里发 `/点歌 雨爱`。首次生成可能因网关冷启动等 2~4 分钟，之后约 1 分钟；生成完成后音频会主动推送过来。

**self-hosted 模式**（用户想用自己的 Replicate Token 才走这条）

1. 先部署 sing-gateway，完整步骤见 <https://github.com/leicanxi/sing-gateway> 的 README。
2. 回插件配置页：运行模式 `self-hosted`；网关地址默认 `http://127.0.0.1:9001/v1`（网关与 AstrBot 同机就不用改）；网关令牌 = 网关启动时的 `SING_GATEWAY_TOKEN`。保存并重载插件。

> ⚠️ `REPLICATE_API_TOKEN` 配在**网关侧**环境变量里，插件配置里**没有**这一项——不要让用户在插件配置里找它。

## 关键配置项（WebUI 配置页，分四组卡片）

- **运行模式** `mode`：`flowbee` / `self-hosted`。
- **FlowBee**：`flowbee_base_url`（保持默认 `https://flowbee.top/v1`）、`flowbee_api_key`。
- **自托管**：`gateway_base_url`、`gateway_token`、`voices`（音色白名单 JSON）、`voices_file`（默认 `~/.octop/sing-voices.json`，需与网关一致）。
- **演唱默认参数** `singing.*`：`voice` 默认音色（点歌不指定就用它；留空用网关默认）、`duration` 片段长度 5~120 秒默认 25、`start` 截取起点默认 60 秒、`pitch` 升降调 ±24 半音默认 0（不升降）、`pitch_mode` 人声音域（`no-change` / `male-to-female` / `female-to-male`）、`transport` 生成方式（保持 `auto`）。
- **使用限制** `limits.*`：`daily_per_user` / `daily_per_group` / `daily_total` 每 24 小时次数、`cooldown_seconds` 连点间隔；`-1` 不限（默认全不限）。窗口是「最近 24 小时」滚动；管理员始终不受限。计数文件在 `data/plugin_data/astrbot_plugin_deepseek_sing/usage.json`。

改配置后需在插件管理里**重载插件**才生效；直接改文件的话在 `data/config/astrbot_plugin_deepseek_sing_config.json`（旧版扁平结构的配置也能兼容读取，配置页重新保存一次即迁移为分组结构）。

## 音色

- 音色广场（公开目录 + 自助上传）：<https://voice-models.flowbee.top>；聊天里发 `/音色` 也会返回这个链接。
- 音色名**不公开枚举**（名字即凭证）：传不存在的音色名会得到 `unknown voice` 报错，接口查不到列表是设计如此，不是故障。让用户去广场核对拼写。
- 想加自定义音色：FlowBee 模式在广场登录后「上传音色」自助提交（自动审核）；self-hosted 模式编辑配置页「音色白名单 JSON」（音色名 → RVC 模型 zip 公网地址），保存重载后写入音色文件、网关即时生效。
- ⚠️ `.pth` 模型文件是 pickle（加载即执行），只建议用户添加可信来源的模型地址。

## 按症状排查

| 症状 | 处置 |
| --- | --- |
| 点歌后很久没回 | 冷启动 2~4 分钟属正常；超 5 分钟看 AstrBot 日志（`data/logs/` 或容器日志）里 `astrbot_plugin_deepseek_sing` 的报错 |
| 提示 VIP 歌没权限/让换歌 | 解锁接口失效，让用户换免费歌；self-hosted 可自配 ncm-api sidecar（见 sing-gateway 仓库） |
| 音频以文件而不是语音发出 | 设计好的降级（平台语音接口失败 → 发文件附件），不是 bug |
| 唱的版本不对（remix/伴奏版） | 让用户回一句「不对，换一版」，插件会给出候选列表重选 |
| 提示已达次数上限 | `limits` 配置在生效；`-1` 恢复不限；管理员不受限 |
| 改了配置不生效 | 保存后需重载插件 |

## 硬性事实

- `/点歌` 和 LLM 工具两条入口都走同一受理点，先生成任务再后台执行、完成后主动推送，不受 AstrBot `tool_call_timeout` 限制。
- 项目仅供学习与技术研究；音色模型版权归原训练者。免责声明与网关协议细节见 README.md，网关部署见 <https://github.com/leicanxi/sing-gateway>。
