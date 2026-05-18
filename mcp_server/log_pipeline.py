"""
log_pipeline.py — Log cleaning and incident grouping.

Ported verbatim from:
  rca_2/backend/log_cleaner.py      → clean_log_line(), fingerprint_for_line()
  rca_2/backend/incident_splitter.py → split_into_incidents(), _infer_error_type()

No logic changes — only reorganized into one file and typed for clarity.

Why these two steps belong together
-------------------------------------
Cleaning must happen before fingerprinting because volatile tokens (UUIDs,
timestamps, IPs) would make every log line produce a unique fingerprint, giving
zero grouping. By stripping them first, two log lines that represent the same
error class produce identical fingerprints regardless of when they occurred or
which request triggered them.

The IncidentPill is the fundamental unit of the RCA pipeline. Every downstream
step — vector search, LLM prompting, report storage — operates on IncidentPills,
not on raw log lines.
"""

import re
import hashlib
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns for volatile tokens that must be stripped before fingerprinting.
# Order matters: more specific patterns should come before more general ones.
# ─────────────────────────────────────────────────────────────────────────────
_CLEAN_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ISO 8601 timestamps (with or without timezone)
    (re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?'), '<ts>'),
    # UUIDs (all standard formats)
    (re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'), '<uuid>'),
    # IPv4 addresses
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), '<ip>'),
    # Hex memory addresses (e.g. 0x7f3a...)
    (re.compile(r'0x[0-9a-fA-F]{4,}'), '<addr>'),
    # trace_id=, request_id=, req_id= followed by alphanumeric value
    (re.compile(r'(?:trace_id|request_id|req_id|correlation_id)=[^\s,;"\']+'), '<id>=<id>'),
    # Long numerics (port-like numbers, sequence IDs — 5+ digits)
    (re.compile(r'\b\d{5,}\b'), '<n>'),
    # Short standalone numerics that look like error codes or counts (4 digits)
    (re.compile(r'\b\d{4}\b'), '<n>'),
]

# Noise patterns — log lines matching these are dropped during incident grouping.
# They represent normal traffic and infrastructure heartbeats, not errors.
_NOISE_PATTERNS = re.compile(
    r'(?:GET|POST|PUT|DELETE|PATCH|HEAD)\s+/.*\s+(?:200|204|301|302|304)\b'
    r'|health.?check'
    r'|heartbeat'
    r'|metrics\s+scraped',
    re.IGNORECASE,
)

# Mapping from lower-case log content keywords → error type codes.
# Used by _infer_error_type() to classify each incident.
_ERROR_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'password\s+authentication|auth(?:entication)?\s+fail', re.I), 'DB_AUTH'),
    (re.compile(r'connection\s+refus|cannot\s+connect|connection\s+timed?\s+out', re.I), 'DB_CONN'),
    (re.compile(r'\b(?:502|503|500|504)\b|bad\s+gateway|service\s+unavailable', re.I), 'HTTP_5XX'),
    (re.compile(r'filenotfound|no\s+such\s+file|file.{0,20}not\s+found|errno\s+2', re.I), 'FILE_IO'),
    (re.compile(r'memoryerror|recursion\s+error|recursionerror|stack\s+overflow', re.I), 'PROC_MEM'),
    (re.compile(r'keyerror|typeerror|attributeerror|nameerror|valueerror', re.I), 'APP_EXC'),
    (re.compile(r'timeout|timed\s+out|deadline\s+exceeded', re.I), 'TIMEOUT'),
    (re.compile(r'jwt|token\s+expired|invalid\s+token|unauthorized', re.I), 'AUTH_TOKEN'),
    (re.compile(r'rate\s*limit|too\s+many\s+requests|throttl', re.I), 'RATE_LIMIT'),
]

# Maps error type codes → human-readable category names (for UI display + vector search).
ERROR_TYPE_TO_CATEGORY: dict[str, str] = {
    'DB_AUTH':    'database-auth',
    'DB_CONN':    'database-connection',
    'HTTP_5XX':   'http-server-error',
    'FILE_IO':    'file-io',
    'PROC_MEM':   'process-memory',
    'APP_EXC':    'application-exception',
    'TIMEOUT':    'timeout',
    'AUTH_TOKEN': 'auth-token',
    'RATE_LIMIT': 'rate-limit',
    'UNKNOWN':    'unknown',
}


@dataclass
class IncidentPill:
    """
    A grouped set of log lines that share the same error fingerprint.

    The fingerprint is a stable hash of the cleaned log content — two log lines
    that describe the same error class always produce the same fingerprint,
    regardless of request-specific tokens (UUID, timestamp, IP).

    The IncidentPill is the fundamental unit passed to the vector DB and LLM.
    """
    incident_id: str            # "{app_id}-{error_type}-{fingerprint[:8]}"
    fingerprint: str            # MD5 of the normalized cleaned content
    error_types: list[str]      # e.g. ["DB_AUTH"]
    category: str               # e.g. "database-auth"
    raw_lines: list[str]        # original log lines (for display)
    cleaned_lines: list[str]    # normalized lines (for embedding + LLM)
    count: int                  # total log lines in this incident
    time_start_iso: str         # earliest timestamp in the incident
    time_end_iso: str           # latest timestamp in the incident
    app_id: str = ""

    def merged_cleaned_text(self) -> str:
        """Concatenate cleaned lines for embedding. Truncated to 8000 chars."""
        return "\n".join(self.cleaned_lines)[:8000]

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "fingerprint": self.fingerprint,
            "error_types": self.error_types,
            "category": self.category,
            "raw_lines": self.raw_lines[:20],   # cap for JSON payload size
            "cleaned_lines": self.cleaned_lines[:20],
            "count": self.count,
            "time_start_iso": self.time_start_iso,
            "time_end_iso": self.time_end_iso,
            "app_id": self.app_id,
        }


def clean_log_line(raw: str) -> str:
    """
    Strip volatile tokens from a raw log line.

    Applies _CLEAN_PATTERNS in order, replacing UUIDs, timestamps, IPs,
    trace IDs, and long numerics with placeholder tokens. The result is
    stable across log lines that describe the same error class.
    """
    cleaned = raw
    for pattern, replacement in _CLEAN_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned.strip()


def fingerprint_for_line(cleaned: str) -> str:
    """
    Produce a stable fingerprint for a cleaned log line.

    Lowercases, strips residual whitespace, then MD5-hashes. Two lines
    that describe the same error (after cleaning) produce the same fingerprint.
    """
    normalized = re.sub(r'\s+', ' ', cleaned.lower()).strip()
    return hashlib.md5(normalized.encode()).hexdigest()


def _infer_error_type(text: str) -> str:
    """Return the first matching error type code, or 'UNKNOWN'.

    First tries to extract error_code from JSON-formatted log lines
    (e.g. {"error_code": "DB_AUTH", ...}), then falls back to regex patterns.
    """
    # Fast path: JSON log lines have a structured error_code field
    ec_match = re.search(r'"error_code"\s*:\s*"([A-Z_0-9]+)"', text)
    if ec_match:
        code = ec_match.group(1)
        if code in ERROR_TYPE_TO_CATEGORY:
            return code
    # Fallback: pattern-match the text
    for pattern, code in _ERROR_TYPE_PATTERNS:
        if pattern.search(text):
            return code
    return 'UNKNOWN'


def split_into_incidents(
    entries: list[dict],
    app_id: str = "",
) -> list[IncidentPill]:
    """
    Group log entries into IncidentPills by error fingerprint.

    Parameters
    ----------
    entries : list of dicts with keys {timestamp: str, raw_line: str, source: str}
    app_id  : identifier for labelling incident IDs

    Returns
    -------
    List of IncidentPill objects, sorted by count descending (most frequent first).

    Algorithm
    ---------
    1. Drop noise lines (200/204 HTTP, healthchecks, heartbeats).
    2. For each error/warning line:
       a. Clean it (remove volatile tokens).
       b. Compute fingerprint of cleaned text.
       c. Append to the group keyed by fingerprint.
    3. For each group, infer error type from the combined text.
    4. Build IncidentPill per group.
    """
    # fingerprint → {raw_lines, cleaned_lines, timestamps}
    groups: dict[str, dict] = {}

    for entry in entries:
        raw = entry.get("raw_line", "")
        ts  = entry.get("timestamp", "")
        # pre_count: set by Splunk-side stats dedup (1 sample represents N originals)
        pre_count = int(entry.get("pre_count", 1))

        # Drop noise
        if _NOISE_PATTERNS.search(raw):
            continue
        # Only keep lines that look like errors/warnings
        if not re.search(r'\b(?:error|exception|fail|critical|warn|fatal)\b', raw, re.I):
            continue

        cleaned = clean_log_line(raw)
        fp = fingerprint_for_line(cleaned)

        if fp not in groups:
            groups[fp] = {
                "raw_lines":    [],
                "cleaned_lines":[],
                "timestamps":   [],
                "fingerprint":  fp,
                "pre_count":    0,   # sum of pre_counts for all entries in this group
            }

        g = groups[fp]
        g["raw_lines"].append(raw)
        g["cleaned_lines"].append(cleaned)
        g["pre_count"] += pre_count  # accumulate actual line counts
        if ts:
            g["timestamps"].append(ts)

    # Build IncidentPill per group
    pills: list[IncidentPill] = []
    for fp, g in groups.items():
        combined_text = " ".join(g["cleaned_lines"])
        error_type = _infer_error_type(combined_text)
        category = ERROR_TYPE_TO_CATEGORY.get(error_type, "unknown")

        timestamps = sorted(g["timestamps"])
        incident_id = f"{app_id}-{error_type}-{fp[:8]}" if app_id else f"{error_type}-{fp[:8]}"

        # Use pre_count if set (Splunk-side dedup), otherwise count raw lines
        real_count = g["pre_count"] if g["pre_count"] > 0 else len(g["raw_lines"])

        pills.append(IncidentPill(
            incident_id=incident_id,
            fingerprint=fp,
            error_types=[error_type],
            category=category,
            raw_lines=g["raw_lines"],
            cleaned_lines=g["cleaned_lines"],
            count=real_count,
            time_start_iso=timestamps[0] if timestamps else "",
            time_end_iso=timestamps[-1] if timestamps else "",
            app_id=app_id,
        ))

    # Most frequent incidents first — they're most useful for batch RCA
    pills.sort(key=lambda p: p.count, reverse=True)
    return pills
