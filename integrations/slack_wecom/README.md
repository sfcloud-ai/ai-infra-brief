# Slack → 企业微信晨报转发器

把一个指定 Slack 私信中的新晨报转发到企业微信群机器人。支持正文和直接附带的 PNG/JPEG 图片，只使用 Python 标准库。

这是用户提供的既有转发器源码，已作为 `sfcloud-ai/ai-infra-brief` 的独立模块整合。运行变更包括接纳符合过滤条件的 `file_share` 晨报、在解析本地 HTTP 请求之前设置超时，以及为图片下载增加覆盖阻塞读取的总超时。注释和文档字符串改为中文。原始导出记录见 `SOURCE_EXPORT.json`，本次变更和原AST指纹见 `INTEGRATION.json`。目录不含实际凭据、账号/服务器配置或运行数据；本次未操作原线上服务，也没有增加自动部署。

## 接入晨报项目

建议保持单一路径：晨报项目生成内容并发到 Slack，由本模块负责转发到企业微信。不要同时从晨报生成器直接向同一微信群发送，以免重复推送。

1. 本目录已位于 `integrations/slack_wecom/`，保持两个 Python 运行文件相邻。当前使用同目录导入，不能只移动其中一个文件；它不随主流水线 wheel 安装，作为独立源码服务维护。
2. 晨报第一行使用 `AI Infra 晨报`，允许日期前缀和 Slack 粗体标记。正文与图片放在同一条消息内，图片应作为 Slack `files` 附件共享。
3. 独立运行本模块，通过配置指定工作区、用户、私信和发送者，不让晨报生成器接触转发凭据。
4. 初次整合先运行离线测试。实际发送测试需另行明确内容和目标，测试不会主动发消息。

模块随主项目使用 Apache-2.0，完整许可证见仓库根目录 LICENSE。`ai_brief.relay_contract.prepare_relay_preview` 提供纯数据适配：首行规范化、直接附件的 `file_share` 事件模板；不会上传或发送。真实采集、模型、Slack上传/发布仍待接入，主流水线 SVG 需先转为 PNG/JPEG。当前模板没有真实消息时间/身份，不是 Slack API 请求，不能据此宣称端到端已连通。

## 自动转发范围

- 只匹配配置中的一个私信、一个发送者和晨报标题。
- 自动接收 `None`、`bot_message` 或 `file_share` 子类型；三者均受相同身份、标题、根消息与编辑过滤约束。
- 普通聊天、历史消息、编辑事件和旧线程回复不自动转发。
- 每次启动或断线恢复都从当前最新消息建立游标，不补发此前消息，包括停机期间的消息。
- 富文本优先使用 Slack `blocks`，保留换行、段落、列表和链接；不支持的结构回退到完整 `text`。
- 只处理同一条匹配晨报的直接图片附件。后补附件、独立的无标题图片消息和任意网页图片地址不在当前能力范围内。

## 配置

将 `config.example.json` 复制为运行目录中的 `config.json`，并填写实际值。示例中的标识均为占位符。

| 配置项 | 含义 |
| --- | --- |
| `team_id` | Slack 工作区 |
| `user_id` | User Token 所属用户 |
| `channel_id` | 唯一允许读取和转发的私信 |
| `sender_id` | 自动晨报的指定发送者 |
| `poll_seconds` | 轮询间隔，30～3600 秒 |
| `forward_images` | 是否启用图片，默认 `false` |

将 `credentials.example.json` 复制为运行目录的 `secrets/credentials.json`，再通过私有编辑器填写已有的 `slack_user_token` 和 `wecom_webhook`。不要将实际文件加入 Git，也不要通过命令参数、聊天或日志传递凭据。

Slack 使用 **User Token**：文字需要 `im:history`，图片额外需要 `files:read`。允许 Slack 隐式附加 `identify`，不接受其他 scope 或 Bot Token。`files:read` 的授权范围大于单个私信，程序中的会话过滤不能缩小 Slack 平台授权范围，启用前应取得账号持有人的同意。

## 在 Linux 上运行

已测试的运行环境是 Linux + Python 3.12。不支持直接在 Windows 上运行服务：文件权限检查、Unix socket、`SO_PEERCRED` 和 `flock` 依赖 Linux/POSIX 能力。

```sh
RELAY_ROOT="$HOME/.local/share/morning-brief-relay"
umask 077
install -d -m 700 "$RELAY_ROOT" "$RELAY_ROOT/secrets" "$RELAY_ROOT/state" "$RELAY_ROOT/run"
cp config.example.json "$RELAY_ROOT/config.json"
cp credentials.example.json "$RELAY_ROOT/secrets/credentials.json"
chmod 600 "$RELAY_ROOT/config.json" "$RELAY_ROOT/secrets/credentials.json"
# 在私有编辑器中填写实际配置和凭据后，再执行：
python -B relay.py preflight --root "$RELAY_ROOT"
python -B relay.py serve --root "$RELAY_ROOT"
```

以上路径须属于运行用户，目录权限为 `0700`，配置和凭据文件权限为 `0600`。`preflight` 会核验 Slack 身份与 scope，并记录当前游标，不发送消息。运行服务只创建本机 Unix socket，不开放 TCP 端口；同一运行目录只允许一个服务实例。

容器部署时，让代码、配置和凭据只读挂载，仅允许 `state`、`run` 写入；使用匹配文件属主的非 root 用户。复用已准备好的镜像，不因运行测试自动安装依赖。

## 手动触发

手动接口只通过本机 Unix socket 使用，并校验调用进程 UID。它与自动轮询分开，不推进自动游标。

手动正文和指定消息入口比自动规则宽：同 UID 调用者可以发送明确指定正文或固定私信中的单条消息，不要求自动晨报标题/发送者。它仍需具体发送授权；不能声称所有手动内容都受自动晨报过滤。保持独立主入口，不能绕过 `main()` 的凭据/目的域校验，将内部 `WeCom` 类直接当任意 webhook 适配器。

向下面命令的标准输入提供 JSON：

```sh
python -B relay.py manual --root "$RELAY_ROOT" --kind text
python -B relay.py manual --root "$RELAY_ROOT" --kind slack-message
python -B relay.py status --root "$RELAY_ROOT"
```

对应输入示例（仅为格式，不要直接当作发送测试执行）：

```json
{"request_id":"operator-example","text":"明确获准发送的正文"}
```

```json
{"request_id":"operator-example-message","message_ts":"1700000000.000001"}
```

```json
{"request_id":"operator-example"}
```

Slack 手动转发也只允许配置中的固定私信。选择线程中的单条消息时可补充 `thread_ts`。重复 `request_id` 返回已保存结果，同一 Slack 消息也会跨手动和自动模式去重。确需重发时，必须使用新的 ID，并提供 `allow_repeat: true` 和非空 `reason`。

## 图片与投递边界

- PNG/JPEG，单张不超过 2,000,000 字节，每条晨报最多 10 张。不缩放或转码。
- 下载只允许 `files.slack.com`，校验路径中的工作区和文件 ID，拒绝跳转和继承的代理。
- 图片只在内存中处理，检查大小、格式结构和尺寸；这不是完整图片解码验证。服务主线程使用 POSIX 定时器限制单次下载总时长为 30 秒，结束后恢复原信号处理器；已有定时器时拒绝下载，避免覆盖其他任务的定时器。
- 先发文字，再逐张发图。文字按 UTF-8 字节数拆分，文字与图片共用至少 3.2 秒的发送间隔。
- 图片失败会记录 `partial`；超时或响应不确定记录 `unknown`，不盲目重试。程序不承诺跨网络调用的 exactly-once。
- SQLite 保存游标、请求标识、内容摘要和投递状态，不保存消息正文、图片或凭据。日志只记录固定事件码。

## 离线验证

在 Linux 下，从本目录运行：

```sh
python -B run_tests.py
# CI 或允许本机 Unix socket 的环境必须完整执行：
python -B run_tests.py --require-unix-socket
```

测试使用合成消息、合成图片、临时状态和假传输层，不读取实际凭据。测试入口通过 Python 审计钩子禁止互联网套接字、DNS 和子进程，并检查外发尝试为零。它不是内核网络沙箱；可在已有 Python 3.12 镜像中配合 `--network none`。普通模式会明确跳过被沙箱阻止的唯一 Unix socket 测试；严格模式不允许此跳过。

覆盖首次启动不回填、过滤、换行保留、字节拆分、去重、未知投递、崩溃恢复、权限校验、恶意图片地址拒绝和图片发送格式等。这些是构造的回归测试，不应称作真实生产问题复现或完整端到端实发验证。
