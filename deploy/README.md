# T4 部署候选与首次交接

最终目标是 main 更新后自动替换旧转发器，始终只有一个实例发送。当前代码是待审查的 CD 候选；T4 已准备独立暂存目录并复用现有 Python 3.12 镜像。尚未安装生产控制入口、创建持久凭据、设置 GitHub Secrets、停止旧实例或启用自动部署。

## 发布路径

cd.yml 仅处理本仓库 push main，仓库 Variable `CD_ENABLED` 必须为 true。每次 main 推送均检查，以免文档提交紧随 Python 提交导致前一部署被当前版本检查跳过。只读测试通过后进入 t4-production 环境，精确构建事件 SHA；传输前再次确认该 SHA 仍是 main 当前版本。外部 PR 只有无凭据测试，不使用 self-hosted runner、pull_request_target 或高权限 workflow_run。

bundle.py 打包锁定哈希的 wheel；gzip 时间及名称、tar 元数据固定。构建设置 SOURCE_DATE_EPOCH。release.py 在非 root、断网容器中验证完整上传，安装至独立 releases/<SHA>/venv，检查晨报入口和已安装转发器能力，然后原子替换暂存指针。wheel 包含转发器、图片处理、游标恢复和受控 runtime。旧版本缺失 ready 标记时，只有不被 current/previous/待切换记录或线上版本引用的目录允许重建。

根权限控制层不随应用自动更新：管理员一次性安装已审查的 stage_helper.py、ssh_entry.py、release.py、policy.py、supervisor.py 与 relay/runtime.py、relay/resume.py。固定配置和代码由 root 持有；main 发布只替换非 root 应用 wheel。修改控制层须重新审查并由管理员安装。

受限 SSH 入口仅接受以下命令，拒绝任意 shell 文本：

```text
status
deploy <正整数工作流序号> <40位提交SHA> <64位包摘要>
rollback <当前工作流序号> <当前版本> <曾成功激活的目标版本>
```

发布和回滚都经同一个入口、root 部署锁及 supervisor。序号高水位拒绝延迟旧事件；相同事件重试必须包身份一致；显式回滚检查当前版本和序号，保留高水位并阻止原事件重放。rollback.yml 仅在 main 手动运行，输入只有目标版本，连接参数一律来自仓库设置。两工作流共用并发组。已经清理或不完整的目标在停止服务前拒绝。

## 单实例切换与回滚

原转发器直接启动会将游标移至最新消息。候选受控入口改为沿用原 cursor，并绑定原 team/user/channel/sender；升级和回滚都使用同一运行根、service.lock、SQLite 账本和私有 socket，不复制一份账本另起发送者，也不回退数据库。

1. 首次切换要求 root 配置显式设置 bootstrap_approved。核对旧容器镜像、UID/GID、状态挂载及源码路径，保存旧源码供受控恢复。旧源码标识是内容摘要，不冒充 Git 提交。
2. 对已受控实例，先通过私有 socket 暂停，等待当前调用完成，确认账本无 sending/unknown，再关闭自动重启并停止。原旧实例没有 drain：冻结它，使用独立只读容器检查账本，再终止冻结进程；检查失败则解冻原实例。此首次操作仍需实际环境预检和当场授权。
3. 确认旧发送者停止后持同一锁，检查 SQLite 完整性、游标及身份。在首次绑定前用 SQLite backup 保存一致性备份；备份先写 partial，再原子落盘，最多保留三个。普通回滚绝不恢复旧账本。
4. 新实例先暂停启动，只读验证 Slack 身份和历史可读性。激活后至少完成一次正常轮询、账本无未知回执才确认健康；不以 mock 晨报检查代替生产健康。激活记录由 root 持有，支持已确认版本的容器重启恢复。
5. 失败时先确认新发送者停止，再用相同账本和受控 resume 入口恢复前版。无法确认停止或存在未知发送结果时保留 pending 并停止自动切换，避免两个发送者或猜测重发。

sending/unknown 必须人工核对真实回执。消息已被删除、历史不可读或超出可完整读取的积压范围时，自动流程不重置游标。网络侧发送结果未知时不能承诺同时零漏发、零重复。

## 容量与隔离

默认最多保留 5 个应用 release、约 1 GiB release 文件，预留 512 MiB 可用空间；清理保护线上、上一版、暂存指针和候选版本。按安装展开量预检容量，属于应用层限制，不是文件系统配额。旧源码快照和共享状态另行管理。控制检查容器超时会清理自身的唯一名称。

验证使用固定镜像摘要、非 root、只读根文件系统、禁用 capabilities、no-new-privileges、CPU/内存/PID 限额、无网络、无 GPU。实际 runtime 只开放出站 bridge 网络，私有配置只读、state/run 可写。部署账户不加入 Docker 组，不获得通用 sudo；其 UID 与转发器运行 UID 分离，不能自行写受保护发布目录或读取运行凭据。

## 待授权的安装与配置

以下步骤尚未执行，须当场确认后由管理员实施：创建专用 SSH 账户/部署 key；安装 root 控制层；配置强制命令公钥、禁止 PTY/代理/端口转发和任意 shell、只允许固定 helper 的 sudo 规则；建立固定 /opt/ai-infra-brief-prototype 和 root 私有 /var/lib/ai-brief-control。将已经核实的实际路径与镜像摘要写入 root 所有的 /etc/ai-brief-stage.json。初始 live_enabled 和 bootstrap_approved 均为 false，首次切换验证通过并获授权后再启用。控制层安装、持续权限配置和首轮切换分别验收。

| GitHub 设置位置 | 名称 | 用途 |
|---|---|---|
| 仓库 Variable | CD_ENABLED | 默认关闭，完成验证后才设 true |
| t4-production Environment Secret | T4_SSH_HOST | 私有地址 |
| 同上 | T4_SSH_USER | 专用受限账户 |
| 同上 | T4_SSH_PRIVATE_KEY | 部署私钥，由用户直接填入 GitHub Settings |
| 同上 | T4_SSH_KNOWN_HOSTS | 经可信通道核验的主机公钥记录 |
| Environment Variable | T4_SSH_PORT | SSH 端口 |

环境分支规则明确只允许 Branch main，保护 main 并限制可信 write 人员。此方案按用户要求不逐次人工审批；拥有 main 写入权限者和组织管理员属于信任边界，不能宣称排除 Owner 或所有其他可信写入者。连接信息不出现在仓库、日志或手动工作流输入中。SSH 原始错误不回显；私钥仅保存于临时权限受限文件，步骤结束清理。不在聊天中提交密钥，不以未经核验的 ssh-keyscan 输出建立信任。

## 验证证据的边界

断网测试包括干净源码分别构建相同 wheel、重复打包、真实 venv 安装和强制中断重试。受限命令解析与 helper 分派、故障回滚和序号防重放使用合成服务驱动；实际子进程和 Unix socket 测试验证暂停/激活/重启、共享锁、游标延续，但外部消息提供方仍是合成适配器。不能把它们描述为真实生产消息交接或完整 SSH 认证链验证。

正式启用仍需完成真实 forced-command/sudo/环境分支限制验收、旧源码及账本兼容性核对、维护窗口内的单实例切换和回滚验证。PR #1 尚不合入，当前线上转发器持续运行。

资料：[GitHub 环境与分支限制](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)、[pip 安全安装](https://pip.pypa.io/en/stable/topics/secure-installs/)。
