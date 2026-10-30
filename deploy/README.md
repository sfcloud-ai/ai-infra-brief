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

这些配置保存在仓库设置中。将 write 权限授予可信成员，并配置 main 分支保护；这些成员可以通过合入主线触发发布，无需逐次审批。

普通 PR 仅运行无凭据测试，不使用生产 Environment。部署工作流只接受本仓库的 main push，在测试通过后读取部署 Secrets；外部 PR 不接触 T4。

## 主机安装

由管理员安装固定控制层和专用部署账户：

1. 将 `stage_helper.py`、`ssh_entry.py`、`release.py`、`policy.py`、`supervisor.py` 与受控 `relay/runtime.py`、`relay/resume.py` 安装为 root 所有的文件。
2. 为部署账户配置 forced-command SSH 公钥和仅允许固定 helper 的 sudo 规则；禁用任意 shell、PTY 和端口转发，不授予通用 sudo 或 Docker 组权限。
3. 建立 `/opt/ai-infra-brief-prototype` 和 root 私有的 `/var/lib/ai-brief-control`，在 root 所有的 `/etc/ai-brief-stage.json` 中指定核实过的路径、运行 UID/GID 和 Python 3.12 镜像摘要。
4. 初始设置 `live_enabled=false`、`bootstrap_approved=false`。在维护窗口核对旧服务与账本，完成单实例交接，设置运行开关并启用自动发布。

固定控制层不随应用 CD 更新；修改控制层需要单独审查并由管理员安装。应用在非 root 容器运行，代码和配置只读，只有 `state`、`run` 可写。部署账户不能直接读取转发凭据或修改受保护的发布目录。

启用转发器独立发布包前，由管理员一并更新固定控制层。受限 `status` 必须返回 `receiver_format: relay-only-v1`，该标识由已安装的接收器提供。Actions 在上传前检查此能力；旧接收器会返回 `administrator_receiver_upgrade_required`，不接收新包。

## 发布

将仓库变量 `CD_ENABLED` 设为 `true` 后，main 中 Python 代码、部署配置、依赖锁定文件或工作流变更会执行发布。工作流构建事件对应的提交，在传输前核对主线部署输入；后续只有 Prompt 或文档变化时继续发布，后续代码已变化时拒绝旧构建。

T4 使用独立的 `sfcloud-ai-relay` wheel，只包含转发器和受控运行入口。日报任务每次从 GitHub main 读取研究和编辑 Prompt；Prompt 不放入 T4 发布包，单独修改 Prompt 或文档也不会触发部署。

```text
main push → 无凭据测试 → 构建 wheel 与离线依赖包
          → 受限 SSH → 校验和安装 → 暂停旧实例
          → 沿用游标启动新实例 → 健康检查 → 确认版本
```

release 使用提交 SHA 标识，依赖按哈希校验，安装在独立虚拟环境中。完整安装后才原子切换指针；中断安装可重试。相同事件重试要求包身份一致，过期事件由部署序号拒绝。

新实例先以暂停状态核验 Slack 身份和历史可读性，激活后至少完成一次正常轮询，且账本无未知回执才确认健康。失败时先确认新实例停止，再恢复前版；不能确认停止或发送结果时暂停切换，等待人工对账。

受控轮询在消息之间处理控制请求，并缓存已读取的批次。临时历史读取失败保留游标重试；投递结果未知或账本异常则暂停。排空与活动健康检查等待上限为 30 分钟，就绪与激活为 3 分钟；受限 SSH 入口的操作上限为 3 小时，工作流留出额外传输和构建时间。排空超时后保留待切换记录，收到排空确认后才停止旧实例。

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
