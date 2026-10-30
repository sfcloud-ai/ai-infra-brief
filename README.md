# AI Infra 晨报

AI Infra 晨报将研究规则、编辑规则、历史去重、报告生成和投递状态分开管理。项目关注模型服务基础设施，以及制造业具身相关的训练技术；规则位于 `ai_brief/resources/prompts/`。

晨报流水线使用本地 **mock/simulation** 模式，从 fixture 生成 JSON、Markdown 和 SVG，并将模拟投递回执保存在本地。默认输入为空 fixture；运行模式配置为 `live` 或未知 adapter 时会报错。

仓库另含独立的 [Slack → 企业微信转发器](integrations/slack_wecom/README.md)。其服务命令会读取私有配置并联网，需单独配置和启动；运行晨报流水线不会启动它。

[Slack 私有频道只读控制](integrations/slack_control/README.md) 提供独立的 `help`、`ping`、`status` 请求与线程回执，默认不启动、不接现有 CD，不转发企微。实际应用身份、频道访问和权限需另行确认。

Python 分发名为 `sfcloud-ai`，模块名为 `ai_brief`，安装后的命令为 `ai-brief`。项目使用 [Apache License 2.0](LICENSE)。文档与代码注释使用中文；变量、函数、类名、配置项等标识符使用英文。

## 安装

使用 Linux 和 Python 3.11 或更高版本。流水线依赖 POSIX `flock`；独立转发器还依赖 Linux Unix socket 和 UID 校验，不能直接在 Windows 上运行。

在源码目录安装运行依赖：

```bash
cd ai-infra-brief
umask 077
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.lock
```

需要安装为命令行工具时，先安装构建依赖，再构建并安装 wheel：

```bash
.venv/bin/python -m pip install --only-binary=:all: -r requirements-dev.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . -w /tmp/sfcloud-wheels
.venv/bin/python -m pip install --no-deps /tmp/sfcloud-wheels/sfcloud_ai-0.1.0-py3-none-any.whl
```

安装后可从其他目录运行 `/absolute/path/to/.venv/bin/ai-brief`。prompt、配置和 schema 随 wheel 打包；修改这些资源后需重新构建并安装。转发器及受控运行入口也随该 wheel 打包，受控命令为 `ai-brief-relay`。

## 配置

| 位置或参数 | 用途 |
| --- | --- |
| `ai_brief/resources/config.json` | 时区、研究窗口、目标条数和历史天数 |
| `ai_brief/resources/prompts/research.md` | 来源、时间窗口与研究范围 |
| `ai_brief/resources/prompts/editor.md` | 编辑、引用、图表与零项结果规则 |
| `ai_brief/resources/schemas/` | 输入、晨报、manifest 和投递记录的数据格式 |
| `run --fixture <path>` | 指定模拟输入；默认使用 `fixtures/empty.json` |
| `--state-dir <path>` | 指定本地运行状态目录，默认 `./var`；放在子命令之前 |
| `run --history-dir <path>` | 显式读取已有晨报历史 |

运行依赖统一维护在 `ai_brief/resources/requirements.lock`，根目录 `requirements.lock` 引用它。状态目录和历史可能包含私人内容，应限制访问权限，不加入 Git。

## 用法

### 生成晨报与模拟投递

```bash
# 按 Asia/Shanghai 当日生成本地模拟报告。
.venv/bin/python -m ai_brief --state-dir ./var run

# 仅写入本地模拟回执；重复执行复用同一回执。
.venv/bin/python -m ai_brief --state-dir ./var simulate-delivery
```

报告写入 `var/runs/YYYY-MM-DD/`，包含 `inputs.json`、`brief.json`、`brief.md`、`overview.svg` 和 `manifest.json`。模拟投递记录为 `delivery.simulation.json`，回执位于 `var/mock-outbox/`。报告带有模拟标记；`simulated` 只表示本地回执，`real_status` 为 `not_sent`。

同一日期再次运行会复用原快照。输入、规则或实现变化，以及产物被修改时，程序拒绝覆盖；需要重新实验时使用新的状态目录。退出码 `0` 表示成功或幂等复用，`2` 表示校验、状态或配置错误。

投递状态为 `prepared → in_flight → simulated`。异常或中断可能留下 `uncertain` 或 `in_flight`，程序不会自动重发，应先核对记录。代码回滚不能用恢复旧账本的方式重置投递状态。

### 指定日期的模拟运行

```bash
.venv/bin/python -m ai_brief --state-dir ./var/replay run \
  --as-of 2026-10-30T08:00:00+08:00 --date 2026-10-30 --allow-backfill
.venv/bin/python -m ai_brief --state-dir ./var/replay simulate-delivery --date 2026-10-30
```

`--date` 必须等于 `--as-of` 对应的 UTC+8 日期。非当日运行必须显式指定 `--allow-backfill`；一次只处理一个日期，不自动补跑缺失日。

### 使用已有历史

```bash
.venv/bin/python -m ai_brief --state-dir ./var run --history-dir /path/to/brief-history
```

默认读取同一状态目录最近 14 天的 `brief.json`。外部历史目录支持符合 brief schema 的 JSON，以及路径带 `YYYY-MM-DD` 或 `YYYYMMDD` 日期的 Markdown；程序读取全文、链接和事件键进行去重。无日期 Markdown、错误 schema、不可读或超过 1 MB 的文件会报错。不会自动导入聊天、Slack 或 Library 历史。

### 接入独立转发器

`ai_brief.relay_contract.prepare_relay_preview` 可将符合 schema 的晨报转换为离线事件模板，首行规范为 `AI Infra 晨报 · 日期`。`event_template` **不是 Slack 发布 API 参数**，`delivery_allowed` 始终为 `false`，调用它不会上传或发送消息。

独立转发器接收固定 Slack 私信、固定发送者的晨报及直接附带的 PNG/JPEG 图片。流水线生成的 SVG 需要先转成 PNG/JPEG，再通过独立实现的 Slack 上传和发布步骤交给转发器。转发器的凭据配置、服务启动、手动入口和投递边界见 [模块用法](integrations/slack_wecom/README.md)。

## 自动部署与回滚

T4 的连接信息在 GitHub 仓库设置中配置，地址和密钥使用 `t4-production` Environment Secrets，端口使用 Environment Variable。仓库变量 `CD_ENABLED` 控制自动发布开关；环境仅允许 `main` 分支。

主线 Python 和部署配置更新通过 Actions 发布转发器，采用单实例切换，升级和回滚沿用同一游标与投递账本。配置、首次交接和回滚操作见 [T4 部署](deploy/README.md)，状态的版本边界见 [状态管理](DEPLOYMENT.md)。

日报任务每次运行从 GitHub 主线读取 [研究 Prompt](https://github.com/sfcloud-ai/ai-infra-brief/blob/main/ai_brief/resources/prompts/research.md) 和 [编辑 Prompt](https://github.com/sfcloud-ai/ai-infra-brief/blob/main/ai_brief/resources/prompts/editor.md)。T4 部署包只包含转发器；单独修改 Prompt 或文档不会触发 T4 部署。
