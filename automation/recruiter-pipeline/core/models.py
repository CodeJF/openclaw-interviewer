from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any


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
    sender: str
    subject: str
    matched_jd_title: str
    score: int
    band: str
    candidate_name: str
    summary: str
    recommendation: str
    raw_result: dict[str, Any]
    work_dir: Path
