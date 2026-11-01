#!/usr/bin/python3
"""仅供 root systemd 使用，随已验证控制版本选择校验器启动代码。"""

import os
from pathlib import Path
import sys


def main():
    if os.getuid() != 0 or sys.argv[1:] not in (['start'], ['stop']):
        raise ValueError('fixed_notice_bootstrap_only')
    # 本文件及同目录信任入口由管理员安装，不位于可自动替换的版本目录。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from stage_helper import current_record
    _, directory = current_record()
    os.execve('/usr/bin/python3', ['/usr/bin/python3', '-s', '-E', '-B',
              str(directory / 'notice_service.py'), sys.argv[1]], {'PATH': '/usr/bin:/bin', 'HOME': '/root'})


if __name__ == '__main__':
    main()
