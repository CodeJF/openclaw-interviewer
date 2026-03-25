# Recruiter Pipeline

当前版本采用 **分层 pipeline**：

1. IMAP 拉取未读邮件（遇到 fetch 阶段 EOF 会自动重连重试）
2. 读取后立刻标记为已读，并验证 `\\Seen` 是否生效
3. 提取附件（PDF / ZIP / TXT / MD）
4. 基于岗位画像的规则预筛（must/bonus/negative + 年限），先缩小候选 JD 范围
5. 所有简历先过预筛；只有达到阈值的简历才进入 LLM，且默认每份简历只比较 Top1 JD（可配置）
6. LLM 默认并发为 2（可配置），避免串行逐封等待过久
7. 每个候选人的 interviewer 调用都使用独立短会话，避免共享长上下文越跑越慢
8. 预筛阈值默认收紧到 42，且多数岗位要求至少命中 2 个 must 词，尽量把明显不匹配的简历挡在 LLM 之前
9. 所有候选人都会产出统一结构化评估结果；通过候选人默认继续归档本地材料
10. 通过名单会额外生成一份带样式的 Excel 报告（含手机号/邮箱/年限/附件/建议），并发送给飞书用户
11. 可选同步到飞书多维表，支持应用身份自管理初始化；按邮件 UID 做 upsert，覆盖通过与未通过候选人
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
- `chat_assistant.py`: 面向 interviewer 飞书机器人的自然语言查询入口
- `core/imap_client.py`: IMAP 收件、已读校验
- `core/resume_parser.py`: 附件解析
- `core/matching.py`: Phase 1 规则/关键词预筛
- `core/reviewer.py`: MiniMax highspeed 精筛
- `core/notifier.py`: 通过 OpenClaw 发送消息
- `core/bitable.py`: 飞书多维表 upsert 同步
- `core/io_ops.py`: 运行时目录、打包等
- `core/query_ops.py`: 招聘查询/执行能力（岗位候选人、未读简历、最近结果、继续处理）

## Run

```bash
bash automation/recruiter-pipeline/run_pipeline.sh
```

当前本地测试批次已调整为 20 封/轮。

## Config

- `pipeline.outputs.archivePassed`: 是否归档通过候选人的本地材料，默认 `true`
- `pipeline.outputs.excelReport`: 是否生成 Excel，默认 `true`
- `pipeline.outputs.zipPackage`: 是否生成 ZIP，默认 `true`
- `pipeline.outputs.notifyFeishu`: 是否发送飞书通知，默认 `true`
- `bitable.enabled`: 是否同步到飞书多维表，默认 `false`
- `bitable.initialization.mode`: `automationManaged` 或 `manual`
- `bitable.initialization.appName` / `tableName`: automation-managed 模式下用于创建或复用资源的名字
- `bitable.initialization.folderToken`: 可选，创建多维表 app 时放入指定云文档目录
- `bitable.initialization.statePath`: 持久化已创建 `appToken/tableId` 的本地状态文件
- `bitable.initialization.appToken` / `tableId`: 可选种子值；automation-managed 模式下可留空，manual 模式下必须提供
- `bitable.uniqueField`: 用于 upsert 的唯一字段，默认 `邮件UID`

## Bitable notes

- 当前仓库保留了 Bitable 相关代码和实验性初始化能力，但**正式业务默认不启用**，以 `config.local.json` 为准。
- 这次验证结果表明：应用身份可以创建并写入自己的 Bitable 资源，但用户前端查看应用资源存在额外可见性/授权问题，不适合作为当前生产主入口。
- 因此当前**推荐生产方案**仍然是：`Excel + ZIP + 飞书消息通知`，不要把多维表格当正式主链路。
- 如果未来再次尝试 Bitable，建议先单独验证：资源归属、用户可见性、记录读写权限，再决定是否启用。

## Dry run

```bash
automation/recruiter-pipeline/.venv/bin/python automation/recruiter-pipeline/run_pipeline.py --dry-run
```

## Next phases

- Phase 2: 将 `core/matching.py` 升级成真正的向量匹配
- Phase 3: 增加人工复核反馈闭环
