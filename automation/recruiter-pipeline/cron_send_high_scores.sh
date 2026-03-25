#!/bin/bash
# 定时筛选简历并发送90分以上的简历PDF
# 执行时间：每天 10:00, 15:00, 17:00

cd /Users/jianfengxu/.openclaw/workspace-interviewer/automation/recruiter-pipeline

# 激活虚拟环境并运行pipeline，筛选90分以上的简历
source .venv/bin/activate

python main.py --config config.highscore.json 2>&1

# 日志记录
echo "$(date '+%Y-%m-%d %H:%M:%S') - 简历筛选任务执行完成" >> /Users/jianfengxu/.openclaw/workspace-interviewer/automation/recruiter-pipeline/runtime/logs/cron.log
