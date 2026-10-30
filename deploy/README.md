# T4 部署

主线更新通过 GitHub Actions 构建独立 release，再经受限 SSH 入口切换转发器。升级与回滚复用同一份游标和投递账本，始终保持单个发送实例。

## GitHub 配置

在仓库 Settings 中创建 `t4-production` Environment，部署分支选择精确的 Branch `main`，并配置：

| 位置 | 名称 | 用途 |
| --- | --- | --- |
| 仓库 Variable | `CD_ENABLED` | `false` 关闭自动发布；`true` 启用 |
| Environment Secret | `T4_SSH_HOST` | T4 地址 |
| Environment Secret | `T4_SSH_USER` | 专用部署账户 |
| Environment Secret | `T4_SSH_PRIVATE_KEY` | 部署私钥 |
| Environment Secret | `T4_SSH_KNOWN_HOSTS` | 经可信通道核验的主机公钥记录 |
| Environment Variable | `T4_SSH_PORT` | SSH 端口 |

这些配置保存在仓库设置中，不作为每次运行的输入。限制可信 write 人员并保护 main；拥有 main 写入权限者和组织管理员属于部署信任边界。自动发布不要求逐次审批。

普通 PR 仅运行无凭据测试，不使用生产 Environment。部署工作流只接受本仓库的 main push，在测试通过后读取部署 Secrets；外部 PR 不接触 T4。

## 主机安装

由管理员安装固定控制层和专用部署账户：

1. 将 `stage_helper.py`、`ssh_entry.py`、`release.py`、`policy.py`、`supervisor.py` 与受控 `relay/runtime.py`、`relay/resume.py` 安装为 root 所有的文件。
2. 为部署账户配置 forced-command SSH 公钥和仅允许固定 helper 的 sudo 规则；禁用任意 shell、PTY 和端口转发，不授予通用 sudo 或 Docker 组权限。
3. 建立 `/opt/ai-infra-brief-prototype` 和 root 私有的 `/var/lib/ai-brief-control`，在 root 所有的 `/etc/ai-brief-stage.json` 中指定核实过的路径、运行 UID/GID 和 Python 3.12 镜像摘要。
4. 初始设置 `live_enabled=false`、`bootstrap_approved=false`。核对旧服务、账本和单实例切换方案后，在维护窗口完成首次交接，再启用自动发布。

固定控制层不随应用 CD 更新；修改控制层需要单独审查并由管理员安装。应用在非 root 容器运行，代码和配置只读，只有 `state`、`run` 可写。部署账户不能直接读取转发凭据或修改受保护的发布目录。

## 发布

将仓库变量 `CD_ENABLED` 设为 `true` 后，每次 main push 都执行发布流程，包括 Python、Prompt 和配置变更。工作流构建事件对应的提交，并在传输前确认它仍是 main 的当前版本。

```text
main push → 无凭据测试 → 构建 wheel 与离线依赖包
          → 受限 SSH → 校验和安装 → 暂停旧实例
          → 沿用游标启动新实例 → 健康检查 → 确认版本
```

release 使用提交 SHA 标识，依赖按哈希校验，安装在独立虚拟环境中。完整安装后才原子切换指针；中断安装可重试。相同事件重试要求包身份一致，过期事件由部署序号拒绝。

新实例先以暂停状态核验 Slack 身份和历史可读性，激活后至少完成一次正常轮询，且账本无未知回执才确认健康。失败时先确认新实例停止，再恢复前版；不能确认停止或发送结果时暂停切换，等待人工对账。

受控轮询在消息之间处理控制请求，并缓存已读取的批次。临时历史读取失败保留游标重试；投递结果未知或账本异常则暂停。排空与活动健康检查使用 30 分钟容忍窗口，就绪与激活为 3 分钟；完整 SSH 操作最多 3 小时，工作流留出额外传输和构建时间。这些是等待预算，不是对网络总耗时的保证。超时不代表命令取消或实例停止；未确认排空时保留待切换记录。

控制请求使用宿主生成的截止时间，激活还需要 root 发布的一次性授权。暂停前撤销授权，激活在就绪检查前后都核验，阻止执行进程退出后迟到的旧请求重新开启发送。

## 回滚

在 Actions 中选择 `t4-controlled-rollback`，从 `main` 运行，只填写仍保留且曾成功激活的目标版本 SHA。连接信息仍从仓库设置读取。发布和回滚共用并发组、受限入口和部署锁。

受限 SSH 入口支持：

```text
status
deploy <工作流序号> <40位提交SHA> <64位包摘要>
rollback <当前工作流序号> <当前版本> <目标版本>
```

回滚校验当前版本和序号，拒绝已清理或不完整的目标，并保留防重放高水位。回滚只恢复代码，**不恢复旧账本或重置游标**。

## 状态与容量

升级使用同一运行根、`service.lock`、SQLite 账本和私有 socket，并绑定原 team/user/channel/sender。首次交接前保存一致性账本备份，最多保留三个。旧版 `relay.py serve` 启动会跳到最新消息，服务交接应使用受控入口。

`sending/unknown` 表示需要核对真实回执，不能自动重发。历史不可读、消息删除或积压超过可完整读取范围时，流程拒绝重置游标。

默认最多保留 5 个应用 release，约 1 GiB release 文件，并预留 512 MiB 可用空间。清理保护线上、上一版、暂存和候选版本；上传和安装前检查容量。共享状态与旧源码快照需要单独管理。
