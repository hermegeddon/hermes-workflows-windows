"""Portable redaction of captured data; never import a provider or host config."""

import re

_VALUES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
]
_SECRET_NAME = r"(?:[a-z0-9]+[_-])*(?:password|passwd|secret|token|api[_-]?key|authorization)(?:[_-][a-z0-9]+)*"
_KEYED = re.compile(
    rf"\b({_SECRET_NAME})\b(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_SECRET_KEY = re.compile(_SECRET_NAME, re.I)


def redact_secrets(text):
    for pattern in _VALUES:
        text = pattern.sub("[REDACTED]", text)
    return _KEYED.sub(lambda m: m[1] + m[2] + "[REDACTED]", text)


def redact(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.fullmatch(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    return value
