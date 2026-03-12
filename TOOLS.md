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

- 对话查询入口：`automation/recruiter-pipeline/chat_assistant.py`
- 典型问题：
  - 查某岗位候选人
  - 查未读简历
  - 查最近一次筛查结果
  - 继续处理 N 封
- 调用示例：
  - `automation/recruiter-pipeline/.venv/bin/python automation/recruiter-pipeline/chat_assistant.py "帮我查 app 主管岗位候选人"`

Add whatever helps you do your job. This is your cheat sheet.
