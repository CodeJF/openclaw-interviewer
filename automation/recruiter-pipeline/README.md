# Recruiter Pipeline

当前版本采用 **分层 pipeline**：

1. IMAP 拉取未读邮件
2. 读取后立刻标记为已读，并验证 `\\Seen` 是否生效
3. 提取附件（PDF / ZIP / TXT / MD）
4. 基于岗位画像的规则预筛（must/bonus/negative + 年限），先缩小候选 JD 范围
5. 所有简历先过预筛；只有达到阈值的简历才进入 LLM，且默认每份简历只比较 Top1 JD（可配置）
6. LLM 默认并发为 2（可配置），避免串行逐封等待过久
6. 解析结果写入 `runtime/cache/parsed/`，重跑时复用，减少重复 PDF 提取
7. 简历与 JD 内容会先裁剪再送模型，降低单次推理耗时
8. 仅接受 `MiniMax-M2.5` 结果，拒绝 OpenAI Codex fallback
9. 通过 `openclaw message send` 发送飞书文本和 zip 结果

## Why

相比“每封简历都全文喂给 agent + 全量 JD”，这版先做预筛：

- 更快
- 更稳
- 更容易控制模型
- 更容易逐步升级到向量匹配

## Files

- `main.py`: pipeline 总入口
- `run_pipeline.py`: 兼容入口，转调 `main.py`
- `core/imap_client.py`: IMAP 收件、已读校验
- `core/resume_parser.py`: 附件解析
- `core/matching.py`: Phase 1 规则/关键词预筛
- `core/reviewer.py`: MiniMax highspeed 精筛
- `core/notifier.py`: 通过 OpenClaw 发送消息
- `core/io_ops.py`: 运行时目录、打包等

## Run

```bash
bash automation/recruiter-pipeline/run_pipeline.sh
```

当前本地测试批次已调整为 20 封/轮。

## Dry run

```bash
automation/recruiter-pipeline/.venv/bin/python automation/recruiter-pipeline/run_pipeline.py --dry-run
```

## Next phases

- Phase 2: 将 `core/matching.py` 升级成真正的向量匹配
- Phase 3: 增加人工复核反馈闭环
