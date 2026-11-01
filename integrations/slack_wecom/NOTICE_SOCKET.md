# 本机通知校验接线

Publisher 与既有转发器保留各自的账号和私有凭据。可选的 Linux Unix socket
服务只调用 `ai-brief-notice-check`；不启动转发器、不开放 TCP 端口、不接收命令或
外部 URL，不发送消息、不读取投递账本、不改变游标。既有校验器仍独立检查固定
私信、连接器身份、完整清单和原图精确字节。

在**原读取账号**下，使用已审查的相同 Python 镜像与固定安装版本运行：

```text
ai-brief-notice-socket --socket /ipc/check.sock --publisher-uid UID --root /relay
```

版本目录与既有转发目录必须只读挂载。仅为 socket 使用独立可写目录，属主为读取
账号、权限为 `0711`。socket 权限为 `0666`，使两个独立账号无需改变用户组即可
建立连接；读取请求或调用校验器之前，内核 `SO_PEERCRED` 必须匹配唯一配置的非
root Publisher UID。文件权限本身不授权请求。不能将此入口暴露给不受信任的
用户命名空间或挂载命名空间。

独立 `notice_client.py` 仅需 Python 3.8+，不读取凭据，也不导入转发器。固定参数：

```text
--socket /run/notice-reader/check.sock --reader-uid UID
```

客户端拒绝符号链接、错误的目录或 socket 属主与权限，以及不匹配的内核对端 UID。
请求限 64 KiB、结果限 16 KiB；校验器最长运行 150 秒，客户端等待不超过 165 秒，
低于 Publisher 适配器的 180 秒上限。错误只返回脱敏的否定结果，不伪造成功。

Publisher 的既有 `notification_checker` 使用固定解释器和参数运行此客户端。
Slack 命令不能选择程序或读取器根目录。Publisher 不获得转发 Token、Docker、sudo
或额外 Slack scope；原有 `NoNewPrivileges` 和私有文件权限保持不变。

本变更只打包可选入口，不安装 socket、不修改 Publisher 配置、不启用通知或日程。
部署前须检查真实 UID 映射、路径属主、容器隔离和状态兼容性，并保留既有投递账本。
残留 socket 会阻止启动；只有确认原读取进程已经停止的监督器才能删除它，不能
直接删除来历不明的活动入口。

离线成功不代表真实 Slack 或企微送达。现有 PNG 探针在预上传 15 分钟后成功完成，
但完整字节 POST 之后的处理移除了 `pHYs` 和 `tEXt` 块，因此原图字节检查仍失败。
应在**新期次源文件的哈希和报告包固定之前**准备图像，再验证该文件的真实传输；
不能放宽字节一致性规则或修改已发布的历史期次。
