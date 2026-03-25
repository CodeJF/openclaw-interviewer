from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any


MAX_REVIEW_CHARS = 12000


class PipelineError(RuntimeError):
    pass


@dataclass
class JDEntry:
    title: str
    path: Path
    content: str


@dataclass
class MailItem:
    uid: str
    message: Message


@dataclass
class ParsedCandidate:
    uid: str
    sender: str
    subject: str
    candidate_name: str
    mail_dir: Path
    attachments: list[Path]
    all_files: list[Path]
    candidate_text: str
    documents: list[dict[str, str]]


@dataclass
class CandidateResult:
    mail_uid: str
    candidate_key: str
    sender: str
    subject: str
    matched_jd_title: str
    score: int
    band: str | None
    passed: bool
    fail_reason: str
    prefilter_passed: bool
    candidate_name: str
    resume_filename: str
    phone: str
    email: str
    years_of_experience: str
    summary: str
    recommendation: str
    processed_at: str
    updated_at: str
    source_task: str
    status: str
    notified: bool
    notes: str
    archive_dir: str
    raw_attachment_paths: list[str]
    evaluation_json: str
    raw_result: dict[str, Any]
    evaluation_path: Path
    work_dir: Path | None
