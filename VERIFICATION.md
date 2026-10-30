# 本地验证记录

验证日期：2026-10-30 UTC。环境：Linux、Python 3.12.14、已有 jsonschema 4.26.0。

## 已执行并通过

- `python -m compileall -q ai_brief tests`
- `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v`：**64项，63通过、1跳过**。跳过项对应原转发器 Unix socket 回归，当前沙箱返回 EPERM；申请扩大执行后环境包装器启动失败，未获得该项运行结果。CI 严格要求该项执行，不将其算作通过。
- CLI 零项干跑：24 → 72 → 168 小时，输出 `zero_items`，不声称做过真实检索。
- 同日时钟前进一小时再次运行：复用完全相同的 manifest/正文。
- 模拟投递连续调用两次：只有一个本地 outbox 回执，`attempts=1`；manifest 的真实投递状态仍为 `not_sent`。
- SVG 已通过 XML 解析，使用 Inkscape 渲染 PNG 后人工视觉检查：文字、零项标识、中文和标签可读，无裁切。
- CI YAML 本地结构解析与只读权限检查通过；**未在 GitHub 执行**。
- 独立只读复核发现并修复两处：过期损坏历史不应阻断新日刊；实现指纹必须覆盖依赖锁文件。增加回归后独立重跑 42 tests 全过，无新增已知问题。
- 后续打包复核发现运行资源未进 wheel，现已修复：prompts、schemas、config、默认 fixture 和标准依赖锁集中在 `ai_brief/resources/` 并配置 package-data。
- 4 项打包回归：实际构建 wheel；安装到新 venv；删除构建源码；切换到独立空目录调用真实 `ai-brief` console script。验证资源/模块及全部锁定运行依赖均来自子 venv、源码与 wheel 指纹一致、零项报告及图生成、重复运行/模拟投递幂等、live adapter 仍失败关闭。
- wheel 内的 `License-Expression: Apache-2.0`、`License-File: LICENSE` 与完整 Apache-2.0 文本均已检查。分发名称为 `sfcloud-ai`。
- 打包/安装回归全程无网络、无用户配置/凭据环境继承。子 venv 设置 `system_site_packages=False`；明确锁定的依赖通过当前执行解释器的 distribution 文件记录离线复制，不复制路径注入文件、入口脚本或 direct_url 元数据。应用只从 wheel 安装。**这是已有依赖的离线复制，不是从软件源全新安装依赖**。
- 额外创建不继承系统包的父 venv：先确认 jsonschema 不可用；仅在该父 venv 加入运行/构建依赖；确认导入路径无 base Python 的 site-packages；再从父解释器重跑 4 项打包回归，全部通过。这验证父 CI venv 中独有的依赖不会因嵌套 venv 而丢失。
- 研究/编辑 prompt 升至 v1.1：不堆统一免责声明，具体选型限制紧邻结论。新增空限制省略、具体限制位置、必要来源归属保留，以及提示词约束的回归测试。
- README、Python 注释/文档字符串和配置文件注释统一使用中文；变量、函数、类名及配置项保持英文。新增 4 项文字/命名检查，未改变对外标识符。
- 用户提供的转发源码已整合；去除注释/docstring 后的AST回归确认，除允许匹配晨报的 `file_share` 子类型外，两个运行文件及原测试逻辑保持一致。
- 原56项转发回归通过审计入口运行：55通过、1项Unix socket受限跳过，外发尝试为0。新增适配回归验证正文首行、file_share文字与PNG、跨重复轮询去重、原过滤边界、恶意附件拒绝和SVG需先栅格化。
- `scripts/check_public_tree.py` 扫描当前源码树未发现标记项；人工检查公开模板与合成/脱敏测试输入，未见真实token、webhook key、IP、账号配置或原始私信正文。扫描不覆盖上游Git历史。空凭据模板保持为空。

当前实现指纹（打包修复后，源码与 wheel 相同）：

```text
48c002b81805cab8dc94ead5b6d2159ef8b5ea7db5a6b2b35105d57126f06a07
```

`examples/zero-item/` 是这次空 fixture 运行产生的报告、JSON 和 SVG，仅用于预览；不含真实研究内容、凭据或投递账本，不应作为新闻发送。

## 本地验证时未执行的操作

本地验证过程未执行联网研究、模型 prompt 调用、真实 Slack/企业微信/邮件发送、读写真实凭据、仓库创建/推送、部署、任何新调度、旧调度变更、真实晨报历史导入、干净环境联网安装或生产可用性验收。独立转发器源码整合不代表主流水线真实采集和发送已接通。对应 adapter 与上线门槛见 README 和 DEPLOYMENT。

2026-10-30 本地验证时点，公开仓库 `sfcloud-ai/ai-infra-brief` 已由独立流程创建，远端仅有 LICENSE，原型代码尚未推送。此处只记录当时状态，不对随后授权的推送或 PR 状态作持续断言。
