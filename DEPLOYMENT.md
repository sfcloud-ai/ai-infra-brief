# 版本化、部署和回滚合同（尚未部署）

## 版本边界

Git 管理代码、prompt、schema、配置与锁定依赖；运行 state、历史、真实收件目的地、凭据都不进 Git。每次运行保存 prompt 内容/哈希、配置、实现指纹与实际读取的历史。**代码回滚不等于投递账本回滚**：不能因为回到旧 commit 而重发今天的晨报。

规则可独立改 prompt/config，通过 PR 看 diff、用历史样本做离线回归，再随 release 更新。schema 的破坏性变更必须显式提升版本并实现状态迁移；当前仅 v1，无迁移器。当前同日已有报告时规则更新会拒绝覆盖，防止悄悄重写已交付内容。

## 将来的 Git 更新流程

不要在运行中的工作目录无条件 `git pull` 后直接重启。候选流程：

1. 用户指定的公开仓库为 `sfcloud-ai/ai-infra-brief`，使用 Apache-2.0；发布前检查不含私人历史/收件信息/凭据。部署机与 state 路径仍须单独确认，公开源码并不授权部署。
2. 取批准的 commit，创建独立 release 目录/独立虚拟环境；不运行未信任 PR 代码于有凭据的主机。
3. 校验 commit、依赖完整 hash、配置/schema；运行单元测试与独立 state 的 mock 干跑；确认真实 adapter 的契约测试。
4. 在无运行实例的边界，原子切换 `current` 指向新 release；运行 state 留在 release 外。
5. 健康检查失败回指旧 release；不要删除或还原当天投递账本。状态格式不兼容时先恢复兼容读取能力，而非强行回滚。

这使 Git pull/Actions 部署均可实现，但本原型没有运行任何上述部署操作。

## GitHub Actions 安全界限

已提供的 workflow **只测试**：普通 `pull_request`、main push、手动触发；`contents: read`；GitHub 托管临时 runner；checkout 不持久化 token；无 secrets、OIDC、部署环境、自托管 runner、`pull_request_target`、`workflow_run` 权限提升或 schedule。

checkout 固定于 [官方 v4.2.2 commit](https://github.com/actions/checkout/commit/11bd71901bbe5b1630ceea73d27597364c9af683)，不是浮动分支。依赖版本源于已验证本地环境，但完整包 hash 和干净联网安装尚未验证，必须在真实上线前补齐。这里也不声称此旧版本永远满足将来的安全要求，后续升级应通过审查。

未来如加部署 workflow，应单独设计：

- 限制可信 write 人员，并保护 main 的合入入口；Python 代码变更合入 main 后自动启动部署，不要求每次人工审批。部署任务只使用 main 的确定 commit，外来 PR 只能无凭据测试。
- T4 地址与密钥持久保存在仓库生产 Environment 的 Secrets 中，非敏感配置放 Variables，不通过每次触发时的输入填写。生产 Environment 仅允许 main 分支使用；部署工作流的执行身份还需通过 Actions 策略限制。
- 不让可修改的 PR artifact、脚本或缓存进入有写权限的部署 job；不要用 `pull_request_target` 执行外来代码。
- 部署权限与研究/Slack 凭据分离；最小权限、短期授权、固定目的地；没有生产身份时失败关闭。
- 签名/校验 release 输入；锁定依赖与 action SHA；留审计记录、上一 release 与可测试回滚步骤。
- 状态必须持久化，不能依赖 GitHub runner 的临时磁盘；在没有可靠共享存储/锁/对账前，不将真实投递放到多 runner 执行。

## 调度切换

确认用户希望的 UTC+8 触发时间，先做影子干跑并比较现有晨报；用户同意切换后停用旧触发器、再启用新触发器，避免双跑。调度器只执行单次 runner，不决定内容，不自动补发过去日期。当前未选择触发时间、未创建任何新调度，也未停止旧任务。

## 上线验收门槛

- 真实研究成功与失败/零项分离，有来源阅读证据、时间与语义去重验证。
- 至少一轮实际 prompt/模型质量评估，符合服务侧与制造业具身侧的筛选边界。
- 图/报告实际可读并核对所有引用；按目标渠道验证文件格式与长度。
- 在批准的测试目的地验证凭据最小权限、上传/发送回执、超时查证、崩溃恢复、防重复和停止开关。
- 有持久状态/权限/备份恢复和独立模拟命名空间；演练发布回滚不重发。
- 用户明确授权真实频道、调度和部署；所有未验证 adapter 仍保持不可执行。
