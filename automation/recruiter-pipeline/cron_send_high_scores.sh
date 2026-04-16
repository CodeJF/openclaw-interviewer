#!/bin/bash
set -euo pipefail
# 定时筛选简历并发送90分以上的简历PDF
# 执行时间：每天 10:00, 15:00, 17:00
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 激活虚拟环境并运行pipeline，筛选90分以上的简历
source .venv/bin/activate

python main.py --config config.highscore.json 2>&1

# 日志记录
mkdir -p "$SCRIPT_DIR/runtime/logs"
echo "$(date '+%Y-%m-%d %H:%M:%S') - 简历筛选任务执行完成" >> "$SCRIPT_DIR/runtime/logs/cron.log"
