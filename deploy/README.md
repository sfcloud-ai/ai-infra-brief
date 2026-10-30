# T4 部署候选与首次交接

这套代码目前用于**独立原型暂存和离线验证**。最终上线目标是新版本接替现有转发器，只有一个实例发送消息；不是长期并行运行两套转发器。现阶段没有停旧实例、复制真实凭据、读取真实账本内容或启用 CD。

## 已确认的环境与实现范围

T4 是 Ubuntu 20.04 / x86_64 / glibc 2.31，宿主机 Python 3.8；存在可复用的 Python 3.12 镜像。验证使用现有镜像，不需要安装宿主机 Python，不给未来部署账户开放 Docker 组权限。现有转发器继续运行。

`bundle.py` 只允许应用 wheel 和锁定哈希的运行依赖，包内没有真实配置、地址、密钥或账本。`release.py` 将包安装到独立的 `releases/<commit>/venv`，检查后用原子 symlink 替换 `current`，保留 `previous`；切换后检查失败时恢复旧指针。崩溃留下的 pending 记录在下次操作前恢复。回滚只改代码指针，不回退状态。

目前健康检查只验证 mock 晨报包能运行，**不能代表真实 Slack→企微链路健康**。发布器不启动或停止系统服务。`stage_e2e.py` 实际执行离线 wheel 安装、切换、幂等和回滚，但使用合成版本号；游标测试使用合成消息，不是真实消息交接的证明。

`.github/workflows/cd.yml` 是暂存候选：仅 `push main`，包括 Python、资源、依赖及部署逻辑变更；精确 checkout 事件 SHA，不接收 PR artifact 或分支输入。只读测试通过后才进入 `t4-prototype` 环境。仓库 Variable `PROTOTYPE_CD_ENABLED` 必须显式为 `true` 才启用，默认未启用；没有手动输入入口。该候选尚未提交到远端，不能当作已经配置的最终生产 CD。

## 预备目录与运行隔离

T4 用户目录中使用专门的 `ai-infra-brief-prototype-staging` 暂存目录；实际私有位置仅保留在本机操作记录，公开代码不写入连接信息。目录包含独立源码、wheel 缓存和原型 release，均不挂载现有 relay 根目录。

验证容器采用现有镜像固定摘要、非 root 用户、只读根文件系统、`--network none`、禁用 Linux capabilities、`no-new-privileges`、CPU/内存/PID 限额、不使用 GPU。源码和依赖只读挂载，只有新建原型目录可写。没有真实 Slack/企微配置或状态进入容器。

## 首次接替旧转发器的必要步骤

直接重启旧入口不满足无漏发要求：原转发器 `baseline()` 总是取最新消息作为游标，主动跳过停机窗口。候选 `relay_resume.py` 提供显式交接模式，沿用已有 cursor，并将其绑定到原 team/user/channel/sender；默认旧行为未修改，必须由受控入口显式选用。

正式切换前须完成以下门槛，当前尚未执行：

1. 记录旧实例精确镜像、命令、挂载和重启策略；只读核对旧源码版本及数据库 schema。定义唯一的运行根、锁文件和私有 socket，禁止新旧实例各自使用一份复制账本并同时运行。
2. 在独立目录准备新代码并运行所有离线验证。冻结自动切换；由管理员确认维护窗口和首轮回滚方案后才允许停旧。
3. 停止旧实例并禁用其自动复活路径，确认进程完全退出并取得同一个 `service.lock`。旧版本没有优雅 drain 接口；首次停机可能中断发送，因此必须检查账本中的 `sending`/`unknown`，并查实际回执。存在未核对结果时停止交接，不能自动补发或标成成功。
4. 停旧并持锁后，使用 SQLite backup API 保存一致性备份，检查 integrity 和兼容 schema。保留原运行根和账本，不把数据写入 release。确认身份一致后写 `handoff_binding_v1`。升级及回滚都保持 cursor、请求去重键、分段与图片状态；备份仅用于灾难恢复，普通代码回滚不能恢复旧账本。
5. 新实例先以暂停投递模式启动并只读核验 Slack 身份/权限、数据库与 socket 状态。确认只有一个实例持锁后再解除暂停。当前候选尚未实现生产暂停/激活 supervisor，这是正式切换前的阻断，不能把 mock 健康检查当作替代。
6. 新实例沿用旧游标取切换期间的消息，使用原请求键去重。API 查询失败或积压超过接口可完整读取的范围时暂停并告警，不重置游标。消息被删除、接口不可读或发送结果未知时无法同时保证不漏发和不重发，必须人工对账。
7. 首轮上线验证失败：先暂停并完全停止新实例，再按原镜像/命令恢复旧代码，仍使用当前账本和受控 resume 入口；禁止直接用会重置 cursor 的旧启动方式。所有自动恢复路径都必须先证明单实例锁与暂停机制有效。

这些步骤的真实服务 supervisor、首次状态核对和暂停/激活演练尚未完成，因此最终“main 合入后自动替换旧 relay”的 CD 不应启用。

## 需要当场确认的持续访问步骤

建议复用现有 Docker 镜像，通过**固定、管理员所有的受限 helper**操作预先指定的原型目录。`ssh_entry.py` 与 `stage_helper.py` 已给出候选实现，仅能运行断网的暂存容器；它们没有控制现有 relay 的权限或命令。未来专用 SSH 账户不加入 Docker 组、不获得通用 sudo 或交互 shell；公钥强制命令只接受 `deploy <40位SHA> <64位摘要>`，禁用 PTY、端口/代理转发。helper 的路径、参数和挂载不可来自任意 SSH 命令文本。

需要当场确认后才可进行的具体安装：创建专用账户及部署 key；将 `ssh_entry.py`、`stage_helper.py`、`release.py` 安装为 root 所有的固定入口；建立 root 所有的 `/etc/ai-brief-stage.json`，只含预先批准的原型路径、非零 UID/GID 与既有镜像摘要；创建该账户所有、0700 的 `/opt/ai-infra-brief-prototype`；添加只允许执行该固定 helper 的 sudoers 规则与 forced-command 公钥。配置校验拒绝其他路径、root 容器用户、浮动镜像标签。以上安装、账户、sudo 规则和授权 key 均尚未执行。生产替换 supervisor 必须另外完成安全审查和单实例交接验证，不能复用暂存成功信号来自动停旧。

GitHub 需管理员配置：

| 位置 | 名称 | 用途 |
|---|---|---|
| 仓库 Variable | `PROTOTYPE_CD_ENABLED` | 暂存开关，准备和验证完成后才设为 `true` |
| `t4-prototype` Environment Secret | `T4_SSH_HOST` | 私有主机地址 |
| 同上 | `T4_SSH_USER` | 专用受限账户 |
| 同上 | `T4_SSH_PRIVATE_KEY` | 专用部署私钥，用户直接在 GitHub Settings 填写 |
| 同上 | `T4_SSH_KNOWN_HOSTS` | 通过可信通道核验的主机公钥记录 |
| Environment Variable | `T4_SSH_PORT` | SSH 端口，非敏感配置 |

环境分支规则必须明确只允许 **Branch `main`**，不要用“仅受保护分支”而不实际保护 main。限制可信 write 人员并保护 main，是这里的信任边界；无需每次人工审批。外部 PR 只有无凭据测试；不用 self-hosted runner、`pull_request_target` 或 `workflow_run` 提权。组织 Owner 仍可修改组织权限，不能宣称仓库设置能排除 Owner。

Secrets 存在环境中且只映射给最后的 SSH 传输步骤。客户端校验 host/user/port，固定主机信任，禁用 agent，不回显 SSH 原始错误；临时私钥文件在结束后删除。不要在聊天中粘贴密钥，不以 `ssh-keyscan` 的未验证结果直接建立信任。

资料依据：[GitHub 部署环境与分支限制](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)、[环境设置](https://docs.github.com/actions/deployment/targeting-different-environments/using-environments-for-deployment)、[并发部署](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments)、[pip 哈希与二进制安装](https://pip.pypa.io/en/stable/topics/secure-installs/)。
