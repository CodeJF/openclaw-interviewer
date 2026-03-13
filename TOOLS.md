# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

## Recruiter Assistant

- 对话查询唯一入口：`automation/recruiter-pipeline/chat_assistant.py`
- 典型问题：
  - 查某岗位候选人
  - 查未读简历
  - 查最近一次筛查结果
  - 继续处理 N 封
  - 把某位候选人简历发我
- 招聘助手对话一律先走该脚本，再基于脚本结果回复
- 不要在查询模式下自己写 IMAP 抓邮件，不要把附件复制到 Desktop，不要临时手搓一套发送流程
- 期望链路：本地命中 → 若无则按需下载单候选人 → 走现有评分体系 → 返回评审结果 → 通过飞书文件发送简历
- 调用示例：
  - `automation/recruiter-pipeline/.venv/bin/python automation/recruiter-pipeline/chat_assistant.py "帮我查 app 主管岗位候选人"`
  - `automation/recruiter-pipeline/.venv/bin/python automation/recruiter-pipeline/chat_assistant.py "把刘艳玲简历发我"`

Add whatever helps you do your job. This is your cheat sheet.
