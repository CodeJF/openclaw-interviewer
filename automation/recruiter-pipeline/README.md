# Recruiter Pipeline

MVP flow for interviewer agent:

1. Pull unseen emails from 263 IMAP inbox
2. Save attachments (PDF / ZIP / TXT)
3. Extract candidate text (currently PDF/TXT/MD)
4. Route candidate to the best JD in `workspace-interviewer/JD`
5. Ask `interviewer` agent for structured score JSON
6. Keep only 80-89 / 90-99 bands
7. Package shortlist ZIP and send to Feishu interviewer bot
8. Mark processed emails as seen and store processed UIDs locally

## Files

- `config.example.json`: template config
- `config.local.json`: local private config (gitignored)
- `run_pipeline.py`: main pipeline
- `run_pipeline.sh`: convenience launcher
- `requirements.txt`: Python deps

`maxEmailsPerRun` defaults to 10 so each run handles a smaller batch instead of chewing through the whole unread backlog in one pass.

## Dry run

```bash
python3 automation/recruiter-pipeline/run_pipeline.py --dry-run
```

## Real run

```bash
bash automation/recruiter-pipeline/run_pipeline.sh
```

The launcher automatically creates `automation/recruiter-pipeline/.venv` and installs `pypdf` there.

## Install daily 08:50 launchd job

```bash
bash automation/recruiter-pipeline/install_launchd.sh
```

This installs `com.hichs.interviewer-recruiter-pipeline` under `~/Library/LaunchAgents/` and schedules it for 08:50 Asia/Shanghai (your Mac local time). It does **not** force an immediate run.

## Runtime outputs

Generated under `automation/recruiter-pipeline/runtime/`:

- `incoming/`: raw downloaded email attachments
- `processed/YYYY-MM-DD/<JD>/<band>/<candidate>/`: shortlisted candidate outputs
- `reports/errors/`: per-mail error logs
- `state/processed-mail-ids.json`: processed UID state
- `outbox/`: generated ZIP packages

## Notes

- Current MVP assumes JD files are plain text files directly under `JD/`
- Current MVP focuses on PDF resumes; ZIP is supported but still only PDF/TXT/MD inside ZIP are extracted
- Feishu delivery uses the configured `interviewer` bot account and direct Feishu OpenAPI file send
