from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .errors import MessageValidationError

MAX_INLINE_FILE_BYTES = 256 * 1024
MAX_MESSAGE_JSON_BYTES = 256 * 1024
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_UNSAFE_TEXT_EXTENSIONS = {
    ".app", ".bat", ".bin", ".bz2", ".cmd", ".command", ".dmg", ".exe",
    ".gz", ".jar", ".js", ".msi", ".pkg", ".ps1", ".py", ".rar", ".sh",
    ".tar", ".tgz", ".vbs", ".xz", ".zip",
}


def message_id(payload: dict[str, Any], delivery_id: str) -> str:
    value = payload.get("message_id") or payload.get("id")
    if value is None and isinstance(payload.get("message"), dict):
        value = payload["message"].get("message_id") or payload["message"].get("id")
    if value is None:
        # Older servers did not include message_id. Delivery id is still a
        # stable idempotency key for those responses.
        value = f"delivery:{delivery_id}"
    value = str(value)
    if not _MESSAGE_ID.fullmatch(value):
        raise MessageValidationError("message_id has invalid format")
    return value


def extract_message(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("message", payload)
    if not isinstance(value, dict):
        raise MessageValidationError("received message is not an object")
    validate_inline_text_file(value)
    return value


def validate_inline_text_file(message: dict[str, Any]) -> None:
    if message.get("type") != "inline_text_file":
        return
    content = message.get("content")
    if not isinstance(content, str):
        raise MessageValidationError("inline_text_file.content must be a UTF-8 string")
    encoding = message.get("encoding", "utf-8").lower().replace("_", "-")
    if encoding in {"utf8", "utf-8"}:
        encoding = "utf-8"
    if encoding != "utf-8":
        raise MessageValidationError("inline_text_file must use UTF-8")
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MessageValidationError("inline_text_file content is not valid UTF-8") from exc
    if len(raw) > MAX_INLINE_FILE_BYTES:
        raise MessageValidationError("inline_text_file exceeds 256 KiB")
    mime_type = message.get("mime_type")
    if mime_type not in {"text/plain", "text/plain; charset=utf-8"}:
        raise MessageValidationError("inline_text_file.mime_type must be UTF-8 plain text")
    digest = message.get("sha256")
    if not isinstance(digest, str) or digest.lower() != hashlib.sha256(raw).hexdigest():
        raise MessageValidationError("inline_text_file SHA-256 does not match content")
    name = message.get("name")
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise MessageValidationError("inline_text_file.name must be a simple file name")
    if len(name.encode("utf-8")) > 255:
        raise MessageValidationError("inline_text_file.name is too long")
    if Path(name).suffix.lower() in _UNSAFE_TEXT_EXTENSIONS:
        raise MessageValidationError("inline_text_file.name uses a blocked executable or archive extension")
    if len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_MESSAGE_JSON_BYTES:
        raise MessageValidationError("serialized message exceeds 256 KiB")


def inline_text_file(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_INLINE_FILE_BYTES:
        raise MessageValidationError("file exceeds 256 KiB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MessageValidationError("file is not valid UTF-8") from exc
    message = {"type": "inline_text_file", "name": path.name, "mime_type": "text/plain; charset=utf-8", "encoding": "utf-8", "content": content, "sha256": hashlib.sha256(raw).hexdigest()}
    validate_inline_text_file(message)
    return message


def decode_message_json(value: str) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MessageValidationError("message JSON is invalid") from exc
    if not isinstance(parsed, dict):
        raise MessageValidationError("message JSON must be an object")
    validate_inline_text_file(parsed)
    return parsed
