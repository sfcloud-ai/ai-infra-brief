"""兼容源码测试入口；生产部署使用单独安装的固定控制层，不随应用 wheel 更新。"""

from integrations.slack_wecom.resume import assert_reconciled, binding, resume_class
