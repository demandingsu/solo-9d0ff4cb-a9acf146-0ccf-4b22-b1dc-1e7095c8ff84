"""运行配置。数据库路径可通过环境变量覆盖，便于测试。"""
from __future__ import annotations

import os
from pathlib import Path

DB_PATH = Path(os.environ.get("TRANSIT_DB_PATH", "/data/transit.db"))

# 收付方向取值
PAY = "pay"        # 付款（钱划出基金会）
RECEIVE = "receive"  # 到账（钱进入对方账户/银行入账）

DIRECTIONS = (PAY, RECEIVE)
