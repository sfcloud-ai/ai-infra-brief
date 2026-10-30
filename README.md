# AI Infra 晨报

AI Infra 晨报的规则、离线生成工具与 Slack → 企业微信转发器。

- 晨报工具按上海时区生成 Markdown、JSON 和 SVG，支持历史去重和本地模拟投递。
- 研究与编辑规则保存在 Prompt 文件中，与配置、代码一起版本化和打包。
- 转发器将指定 Slack 私信中的晨报正文和 PNG/JPEG 附件转发到企业微信。
- GitHub Actions 支持主线发布、单实例切换和受控回滚。

晨报命令使用本地 fixture，不联网采集、调用模型或发送消息；默认 fixture 为空。真实消息转发使用独立的转发器入口和私有配置。

## 快速开始

使用 Linux 和 Python 3.12：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements-dev.lock

# 生成当日模拟晨报
.venv/bin/python -m ai_brief --state-dir ./var run

# 写入本地模拟回执，重复执行不会重复投递
.venv/bin/python -m ai_brief --state-dir ./var simulate-delivery
```

报告保存在 `var/runs/<日期>/`，模拟回执保存在 `var/mock-outbox/`。同一天重复运行会复用已有报告；输入或规则变更后需要使用新的状态目录。历史回放可通过 `run --as-of <带时区的时间> --date <日期> --allow-backfill` 指定单日。

可用 `run --history-dir <目录>` 读取已有晨报。历史文件支持符合 schema 的 JSON，以及路径中带日期的 Markdown；程序按 URL 和事件键去重。

## 规则与配置

| 文件 | 用途 |
| --- | --- |
| [research.md](ai_brief/resources/prompts/research.md) | 研究范围、来源和时间窗口 |
| [editor.md](ai_brief/resources/prompts/editor.md) | 编辑标准、引用和图表要求 |
| [config.json](ai_brief/resources/config.json) | 时区、窗口、条数和历史范围 |
| [schemas](ai_brief/resources/schemas/) | 输入、报告和投递数据格式 |

运行资源随 wheel 安装，修改后重新构建发布。Python 分发名为 `sfcloud-ai`，晨报命令为 `ai-brief`，受控转发器命令为 `ai-brief-relay`。

## 转发与部署

- [转发器用法](integrations/slack_wecom/README.md)：Slack 和企业微信配置、正文与图片转发、手动入口。
- [T4 部署](deploy/README.md)：GitHub Secrets/Variables、主线自动部署和回滚。
- [状态管理](DEPLOYMENT.md)：规则更新、运行数据与账本的版本边界。

地址、密钥、运行状态和私人历史保存在仓库之外。T4 连接信息在 GitHub 仓库设置中配置，无须每次触发时填写。

## 开发

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -B integrations/slack_wecom/run_tests.py --require-unix-socket
```

README 和代码注释使用中文；变量、函数、类名和配置项使用英文。

## 许可

[Apache License 2.0](LICENSE)。
