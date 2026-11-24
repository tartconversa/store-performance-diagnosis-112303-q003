"""通用工具：时间、哈希、校验与审计。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import APP_NAME


def now_iso() -> str:
    """带时区的当前时刻（秒精度，UTC）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(text: str) -> date:
    return date.fromisoformat(text)


def parse_datetime(text: str) -> datetime:
    """接受 ISO 日期或日期时间；纯日期按当天 00:00 处理。"""
    if "T" in text or " " in text:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        value = datetime.combine(date.fromisoformat(text), datetime.min.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def iso_date(value: datetime | date) -> str:
    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()


def add_days(text: str, days: int) -> str:
    return (parse_date(text) + timedelta(days=days)).isoformat()


def canonical_json(value: Any) -> str:
    """稳定序列化，用于哈希和证据快照。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, entity_type, entity_id, detail) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (now_iso(), actor, action, entity_type, entity_id, canonical_json(detail or {})),
    )


def record_event(
    conn: sqlite3.Connection,
    table: str,
    *,
    id_column: str,
    entity_id: str,
    actor: str,
    event_type: str,
    from_state: str | None = None,
    to_state: str | None = None,
    note: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    """向 diagnosis_event / action_event 写入一条状态变化记录。"""
    if table not in {"diagnosis_event", "action_event"}:
        raise ValueError(f"不支持的事件表: {table}")
    conn.execute(
        f"INSERT INTO {table}({id_column}, at, actor, event_type, from_state, "
        "to_state, note, payload) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entity_id,
            now_iso(),
            actor,
            event_type,
            from_state,
            to_state,
            note,
            canonical_json(payload or {}),
        ),
    )


def stable_id(prefix: str, *parts: Any) -> str:
    """由内容派生稳定编号，避免重复导入产生重复记录。"""
    digest = sha256_text("|".join(str(p) for p in parts))[:10]
    return f"{prefix}-{digest}"


def project_identity() -> dict[str, str]:
    return {"app": APP_NAME}
