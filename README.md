# AI Infra 晨报流水线原型

公开仓库：`sfcloud-ai/ai-infra-brief`。Python 分发名为 `sfcloud-ai`；仓库名称与分发名称不同。

**目前是可测试的本地原型，不是已部署的晨报服务。** 默认晨报流水线使用空 fixture，不会联网、调用模型、读取凭据、发 Slack/邮件、创建调度或补发过去晨报。另包含用户提供的独立 Slack→企业微信转发器源码；其真实服务命令会读取私有配置并联网，未自动启动、部署或接入。默认运行和回执均明确标注 mock/simulation。

目标是把未来每日晨报分成可版本化的规则与代码，而不是把整段任务硬塞进一个定时器：

```text
外部触发器（尚未安装）
  → 日期/锁/已生成检查
  → research.md + 明确的历史/窗口输入 → ResearchAdapter
  → 24h → 不足再 72h → 不足再 168h；来源/时间/去重
  → editor.md + 合格项 → EditorAdapter → JSON Schema
  → brief.json + brief.md + overview.svg + manifest.json
  → 独立投递状态机（当前仅本地模拟）
```

## 快速运行

已验证环境：Linux、Python 3.12.14、`jsonschema==4.26.0`。代码要求 Python ≥3.11；文件锁依赖 POSIX `flock`，Windows 未验证。源码运行和 wheel 安装后从无关目录运行均已验证；运行资源随 wheel 打包，不依赖当前目录或源码副本。

```bash
cd ai-infra-brief
# 如需新环境：
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements-dev.lock

# 本地零项干跑，以 Asia/Shanghai 当日分目录；不会真实研究或发送。
.venv/bin/python -m ai_brief --state-dir ./var run

# 只向 var/mock-outbox 写模拟回执。重复执行仍为一条，不会真实投递。
.venv/bin/python -m ai_brief --state-dir ./var simulate-delivery
.venv/bin/python -m ai_brief --state-dir ./var simulate-delivery

.venv/bin/python -m unittest discover -s tests -v
```

若依赖已装，可直接用 `python` 替换 `.venv/bin/python`。退出码 `0` 表示成功或幂等复用；`2` 表示校验/状态/配置错误。任何未知/live adapter 都失败关闭，不读取凭据，更不会静默改用 mock。

### 安装 wheel 后独立运行

```bash
# 在源码目录构建；先按上文安装 requirements-dev.lock。
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . -w /tmp/sfcloud-wheels
.venv/bin/python -m pip install --no-deps /tmp/sfcloud-wheels/sfcloud_ai-0.1.0-py3-none-any.whl
# 之后可在任何可写目录运行，不需要保留源码目录。
/absolute/path/to/.venv/bin/ai-brief --state-dir /path/to/private-state run
```

发布的 Python 分发名称为 `sfcloud-ai`，模块名称仍为 `ai_brief`，命令为 `ai-brief`。资源的唯一维护位置是 `ai_brief/resources/`；修改规则后重新构建并安装新 wheel。根目录 `requirements.lock` 只是指向包内标准锁文件的入口，避免两份依赖定义漂移。

### 显式复现某一天（仅模拟）

```bash
python -m ai_brief --state-dir ./var/replay run \
  --as-of 2026-10-30T08:00:00+08:00 --date 2026-10-30 --allow-backfill
python -m ai_brief --state-dir ./var/replay simulate-delivery --date 2026-10-30
```

`--date` 必须等于 `--as-of` 对应的 UTC+8 日期。非当日必须显式 `--allow-backfill`；一次只处理一个指定日期，不扫描、不自动补跑缺失日。

## 文件和规则

| 文件 | 职责 |
|---|---|
| `ai_brief/resources/prompts/research.md` | 研究标准、来源验证、24–72h/一周窗口、服务侧/训练侧边界 |
| `ai_brief/resources/prompts/editor.md` | 编辑标准、引用、图表约束、零项与失败区分 |
| `ai_brief/resources/config.json` | UTC+8、窗口、目标条数和历史天数 |
| `ai_brief/resources/schemas/*.schema.json` | candidate、brief、manifest、delivery 四份数据合同 |
| `ai_brief/adapters.py` | 研究、编辑、投递接口；当前仅 mock 实现 |
| `ai_brief/pipeline.py` | 流水线、去重、原子落盘和保守投递状态机 |
| `ai_brief/relay_contract.py` | 纯数据适配：输出符合接收侧标题/文件消息结构的离线事件模板，不上传或发送 |
| `integrations/slack_wecom/` | 复用用户提供的独立转发器、空配置模板与原56项回归测试 |
| `scripts/check_public_tree.py` | 离线公开源码扫描，不回显疑似敏感值 |
| `ai_brief/resources/fixtures/empty.json` | 默认真实空输入，不伪造新闻 |
| `tests/test_pipeline.py` | 合成 fixture 仅用于测试，域名统一 example.invalid |
| `tests/test_packaging.py` | 实际构建/安装 wheel；删除构建源码后，从独立目录调用 console script |
| `.github/workflows/ci.yml` | 待上库的只读 mock 测试，无部署/调度 |
| `DEPLOYMENT.md` | Git 更新、原子发布/回滚与未来部署的边界 |

需求已写入 prompt：**模型服务优先；不同模型/服务不共享权重，不能将共享权重作为收益前提；训练聚焦制造业具身**。达到目标条数仍不能替代质量判断；当前质量判断仅是 fixture 输入，真实模型与来源核验未接入。

### 长期写作与代码约定

- 真实晨报不堆“未独立复现”“作者报告”等统一免责声明；影响选型的具体限制紧邻对应结论说明。没有实质限制时 `limitations` 可以为空，不为凑格式写套话。来源记录、关键不确定性和模拟模式标记仍保留。
- README 和代码注释使用中文；变量、函数、类名和配置项使用英文，不使用中文标识符。命名与文字约定都有回归测试。

### 与既有转发器的适配接口

```python
from ai_brief.relay_contract import prepare_relay_preview

# brief 是通过 schema 校验的晨报 JSON；不会触发外部请求。
preview = prepare_relay_preview(brief, attached_files=[])
text = preview["event_template"]["text"]
```

适配器将首行规范为 `AI Infra 晨报 · 日期`，不把 Markdown 的 `#` 原样发送。文件元数据来自将来 Slack 上传完成后的直接附件事件，生成模板时带 `file_share`；接收侧只增加对此子类型的兼容，仍检查固定发送者、标题、根消息和未编辑状态，并复用原附件 URL/格式/大小校验。假传输层已验证文字→PNG、重复不重发和恶意附件拒绝。

`event_template` 是**离线接收事件模板，不是 Slack 发消息 API 参数**；不编造真实消息时间或发送者，`delivery_allowed` 始终为 `false`。主流水线的真实采集、模型调用、Slack 图片上传/发布仍待接入；当前 SVG 也不能直接传给只接受 PNG/JPEG 的转发器，须先明确栅格化与上传步骤。独立转发器的运行/权限与手动入口边界见 [模块 README](integrations/slack_wecom/README.md)。

### 历史晨报

默认读取同一 state 目录最近 14 天已生成 `brief.json`。既有晨报可显式挂载只读目录：

```bash
python -m ai_brief --state-dir ./var run --history-dir /path/to/brief-history
```

目录可包含符合 brief schema 的 JSON、路径带 `YYYY-MM-DD` 或 `YYYYMMDD` 的 Markdown。Markdown 读取全文并提取 HTTP(S) 链接；JSON 同时取事件键。当前程序做 URL（去追踪参数/fragment）和事件键的传递闭包去重，**语义去重仍待真实模型接入**。请只放晨报文件；无日期 Markdown、错误 schema、不可读或超过 1 MB 的历史文件会报错，而非静默跳过。未自动导入现有聊天、Slack 或 Library 晨报。

## 每日输出与投递状态

```text
var/
  .pipeline.lock
  runs/2026-10-30/
    inputs.json                # prompt/config/fixture/已读历史与上下文快照
    brief.json                 # schema 校验后的模拟正文
    brief.md                   # 人可读报告，显著“模拟”标签
    overview.svg               # 方向计数图；不是性能图或真实新闻统计
    manifest.json              # 内容哈希、窗口、排除原因、prompt/实现指纹
    delivery.simulation.json   # 与真实发送彻底分开的本地状态
  mock-outbox/<幂等键>.json
```

- 同一日期只生成一次。再次调用复用原快照，即使时钟前进也不重新取内容；原产物被修改或输入/prompt/config/实现变化则拒绝覆盖，显式新实验请换 state 目录。
- 日期目录先在 staging 中完整写入，再原子 rename。失败不留下可投递的半份报告。已完整落盘的内容 hash 会在复用/模拟投递前再次检查。
- 投递幂等键绑定日期 + simulation + 固定 local-mock 目的地，账本同时绑定正文 hash。
- 状态：`prepared → in_flight → simulated`。发送前先持久化 `in_flight`；异常变为 `uncertain`；中途崩溃保留 `in_flight`。两者都禁止自动重发，需先查回执并人工对账。
- `simulated` 只意味着本地文件回执。manifest 中 `real_status` 永远是 `not_sent`。没有宣称 Slack exactly-once；真实跨系统 exactly-once 不能仅靠本地账本保证。
- state/历史可能含私人内容；不进入 Git、不上传 CI artifact。生产须设置目录权限、加密/备份、留存策略及可靠共享锁；本地锁不是多机分布式锁。

## 测试范围与明确缺口

源码包初始验证包含64项主套件：63项通过，1项因当时的本地沙箱禁止 Unix socket 而跳过；内嵌原转发器56项中，55项通过，同一 Unix socket 项跳过。CI 使用 `--require-unix-socket` 强制该项执行，不能把跳过算作通过。本次提交前复核另补充了不完整本地 HTTP 请求和慢速图片读取的真实本地 I/O 回归；最终提交的执行结果见 PR。其他覆盖包括流水线、数据契约、图文适配、去重、来源代码保持、中文注释与英文标识符，以及 wheel 安装回归。

发布前单独对干净的候选源码运行 `python -B scripts/check_public_tree.py`。普通单元测试只验证扫描器行为，不把本地已生成且被 Git 忽略的 `var/` 状态误当成待发布文件；发布扫描仍会拒绝候选目录中的状态、凭据与其他敏感文件。

安装回归使用不继承系统包的子 venv；从当前执行解释器复制明确锁定的已安装运行依赖，再离线安装应用 wheel。另已从依赖仅存在于父 venv 的环境重跑打包回归。没有联网安装，不代表所有依赖从全新软件源安装已验证。

**尚未验证或未实现：**

1. 真实搜索/网页读取/发布时间与证据核验；实时来源不可用时的重试、费用与超时策略。
2. 真实模型运行两份 prompt、语义去重、编辑质量评估和事实一致性评估。
3. 主流水线真实 Slack 身份/频道/文件上传/发布与回执对账；独立转发器源码和离线适配测试不等于这条发送链已连通，未申请 token 或权限。
4. 正式图像/PDF/Docx/Library 产物链。本原型只生成 Markdown + JSON + SVG；不是生产版图文晨报。
5. 真实 CI 执行、全新环境网络安装、依赖 artifact hash 锁、多 Python/平台验证。
6. 外部每日调度、部署、监控报警、加密历史、状态迁移、跨机锁。

这些缺口不能用 mock 测试通过来替代。2026-10-30 本地验证时，远程仓库 `sfcloud-ai/ai-infra-brief` 已创建且仅含 LICENSE，原型尚未推送。这是验证时点记录；后续发布状态以远端为准。本地验证没有执行部署、添加定时任务或修改现有晨报任务。

## 开源许可

本项目使用 **Apache License 2.0**，完整条款见 [LICENSE](LICENSE)。Python 分发元数据包含 `License-Expression: Apache-2.0`，wheel 内附完整 LICENSE。项目不包含个人晨报历史、真实收件目的地、凭据或运行状态；默认样例只有明确标注的零项模拟结果。
