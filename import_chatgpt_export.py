#!/usr/bin/env python3
"""Import conversations from ChatGPT data export JSON into Hermes LCM.

Reads conversations-*.json files from a ChatGPT data export directory and imports
them into the Hermes LCM SQLite database. Modeled after import_lossless_claw.py —
dry-run is the default, --apply triggers actual writes.

Usage:
    # Dry-run (default):
    python import_chatgpt_export.py --source-dir /path/to/chatgpt-export --target-db ~/.hermes/lcm.db

    # Apply:
    python import_chatgpt_export.py --source-dir /path/to/chatgpt-export --target-db ~/.hermes/lcm.db --apply

    # JSON output:
    python import_chatgpt_export.py --source-dir /path/to/chatgpt-export --target-db ~/.hermes/lcm.db --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hermes_lcm"


def _ensure_local_package_importable() -> None:
    """Make local plugin modules importable when this file is run directly."""
    if PACKAGE_NAME in sys.modules:
        return
    pkg = types.ModuleType(PACKAGE_NAME)
    pkg.__path__ = [str(PLUGIN_DIR)]
    pkg.__package__ = PACKAGE_NAME
    sys.modules[PACKAGE_NAME] = pkg


_ensure_local_package_importable()

from hermes_lcm.config import LCMConfig  # noqa: E402
from hermes_lcm.ingest_protection import protect_message_for_ingest  # noqa: E402
from hermes_lcm.message_content import normalize_content_value  # noqa: E402
from hermes_lcm.store import MessageStore, _normalize_source_value  # noqa: E402
from hermes_lcm.tokens import count_message_tokens  # noqa: E402


# ── Content types to skip entirely (internal UI artifacts) ──────────────────

SKIP_CONTENT_TYPES = frozenset({
    "tether_browsing_display",  # Search/browse UI overlay
    "tether_quote",             # File search quote display
})

# Content types that are usually empty (thinking/reasoning) — skip if no content
THINKING_CONTENT_TYPES = frozenset({
    "thoughts",
    "reasoning_recap",
})


# ── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChatGPTImportCandidate:
    """A single message ready for insertion into Hermes LCM."""
    source_message_id: str       # ChatGPT message UUID
    source_conversation_id: str  # ChatGPT conversation UUID
    source_session: str          # Human-readable session label
    target_session_id: str       # Full provenance string: namespace:conversation:uuid
    source: str                   # Same as target_session_id (for source column)
    role: str
    content: str
    tool_call_id: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_name: str | None
    timestamp: float
    token_estimate: int
    ordinal: int                  # Position within conversation


@dataclass
class ImportResult:
    source_dir: str
    target_db: str
    import_id: str
    files_scanned: int = 0
    conversations_scanned: int = 0
    messages_scanned: int = 0
    eligible: int = 0
    would_import: int = 0
    imported: int = 0
    skipped_existing: int = 0
    skipped_empty: int = 0
    skipped_system: int = 0
    skipped_type: int = 0
    conversations_imported: int = 0
    backup_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir": self.source_dir,
            "target_db": self.target_db,
            "import_id": self.import_id,
            "files_scanned": self.files_scanned,
            "conversations_scanned": self.conversations_scanned,
            "messages_scanned": self.messages_scanned,
            "eligible": self.eligible,
            "would_import": self.would_import,
            "imported": self.imported,
            "skipped_existing": self.skipped_existing,
            "skipped_empty": self.skipped_empty,
            "skipped_system": self.skipped_system,
            "skipped_type": self.skipped_type,
            "conversations_imported": self.conversations_imported,
            "backup_path": self.backup_path,
        }


# ── Helper functions ────────────────────────────────────────────────────────

def _compound_key_to_int(*parts: str) -> int:
    """Convert one or more strings into a deterministic integer for the tracking table.

    The lcm_imported_messages table uses INTEGER for source_message_id and
    source_conversation_id. ChatGPT uses UUID strings, and some message IDs
    appear in multiple conversations (shared system prompts). We join the parts
    with a separator, then hash to get stable, unique integers.
    """
    compound = "\x00".join(parts)
    digest = hashlib.sha256(compound.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**63 - 1)


def _default_import_id(source_dir: Path) -> str:
    """Generate a stable import ID from the source directory path."""
    return hashlib.sha256(str(source_dir.resolve()).encode("utf-8")).hexdigest()[:16]


def _parse_timestamp(value: Any, fallback: float) -> float:
    """Parse a timestamp from ChatGPT export format (Unix seconds with microseconds)."""
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return fallback
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _walk_conversation(mapping: dict, current_node: str | None) -> list[dict]:
    """Walk a ChatGPT conversation tree and return an ordered list of message nodes.

    ChatGPT conversations are tree-structured (supporting edits/branching).
    The ``current_node`` field points to the tip of the active branch.
    We walk backwards from current_node to root, then reverse, to get the
    linear conversation that the user actually experienced.

    Returns only nodes that have a non-null ``message`` field (skipping the
    root sentinel).
    """
    # ── Find the root sentinel (parent is null or "client-created-root") ──
    root_id = None
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if parent is None or parent == "client-created-root":
            if node.get("message") is None:
                root_id = node_id
                break

    # ── Determine the tip of the active branch ──
    if current_node and current_node in mapping:
        tip = current_node
    elif root_id:
        # Fallback: walk from root to the last node following first children
        tip = root_id
        visited = set()
        while tip in mapping and tip not in visited:
            visited.add(tip)
            children = mapping[tip].get("children", [])
            if children:
                tip = children[0]
            else:
                break
    else:
        return []

    # ── Walk backwards from tip to root ──
    path_ids: list[str] = []
    node_id = tip
    visited: set[str] = set()
    while node_id and node_id in mapping and node_id not in visited:
        visited.add(node_id)
        path_ids.append(node_id)
        parent = mapping[node_id].get("parent")
        if parent is None or parent == "client-created-root":
            break
        node_id = parent

    path_ids.reverse()

    # ── Collect nodes with actual messages ──
    result = []
    for nid in path_ids:
        node = mapping.get(nid, {})
        if node.get("message") is not None:
            result.append(node)
    return result


def _extract_content(msg: dict) -> str:
    """Extract text content from a ChatGPT message object.

    Handles:
    - String parts (most common)
    - Empty string parts (skip)
    - Dict parts with image_asset_pointer (reference, don't embed)
    - Dict parts with other content_type (skip binary, keep text)
    """
    content = msg.get("content", {})
    if not content:
        return ""

    parts = content.get("parts", [])
    if not isinstance(parts, list):
        # Edge case: parts is not a list — treat content as a string
        return str(content.get("content_type", "")) if content else ""

    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if part:  # skip empty strings
                text_parts.append(part)
        elif isinstance(part, dict):
            ct = part.get("content_type", "")
            if ct == "image_asset_pointer":
                asset = part.get("asset_pointer", "image")
                # Keep a human-readable reference
                text_parts.append(f"[[{asset}]]")
            elif ct in ("code_execution_result",):
                # Code execution output
                output = part.get("output") or part.get("text", "")
                if output:
                    text_parts.append(str(output))
            # Skip other dict content types (binary blobs, etc.)

    return "\n".join(text_parts)


def _extract_tool_info(msg: dict, role: str) -> tuple[str | None, list[dict] | None, str | None]:
    """Extract tool call information from a ChatGPT message.

    Returns: (tool_call_id, tool_calls, tool_name)
    """
    tool_call_id = None
    tool_calls = None
    tool_name = None

    metadata = msg.get("metadata") or {}
    author = msg.get("author") or {}
    author_name = author.get("name")

    if role == "tool" and author_name:
        # Tool result messages have author.name set to the tool name
        tool_name = author_name
        # Try to get tool call ID from various metadata fields
        tool_call_id = (
            metadata.get("tool_call_id")
            or metadata.get("message_tool_use_id")
            or f"chatgpt_tool_{msg.get('id', 'unknown')}"
        )

    if role == "assistant":
        # Check for invoked function/tool_use in metadata
        invoked = metadata.get("invoked_function")
        if invoked and isinstance(invoked, dict):
            func_name = invoked.get("name", invoked.get("function", {}).get("name", "unknown"))
            func_args = invoked.get("arguments") or invoked.get("function", {}).get("arguments", {})
            if isinstance(func_args, str):
                args_str = func_args
            else:
                args_str = json.dumps(func_args, ensure_ascii=False, separators=(",", ":"))
            tool_calls = [{
                "id": invoked.get("id", f"chatgpt_tool_{msg.get('id', 'unknown')}"),
                "type": "function",
                "function": {
                    "name": str(func_name),
                    "arguments": args_str,
                },
            }]

    return tool_call_id, tool_calls, tool_name


# ── Main collection logic ───────────────────────────────────────────────────

def _collect_candidates(
    conversations: list[dict],
    *,
    namespace: str = "chatgpt-export",
) -> tuple[list[ChatGPTImportCandidate], dict[str, int]]:
    """Parse ChatGPT conversations into import candidates.

    Returns (candidates, stats) where stats contains skip counts.
    """
    candidates: list[ChatGPTImportCandidate] = []
    stats = {
        "conversations": 0,
        "messages_scanned": 0,
        "skipped_empty": 0,
        "skipped_system": 0,
        "skipped_type": 0,
    }
    conversation_ids: set[str] = set()

    for conv in conversations:
        conv_id = conv.get("id") or conv.get("conversation_id", "")
        if not conv_id:
            continue

        conversation_ids.add(conv_id)
        conv_create_time = conv.get("create_time")
        fallback_ts = _parse_timestamp(conv_create_time, time.time())
        mapping = conv.get("mapping", {})
        current_node = conv.get("current_node")

        # Walk the tree to get ordered messages
        nodes = _walk_conversation(mapping, current_node)

        source_session = conv_id
        source = f"{namespace}:conversation:{source_session}"

        ordinal = 0
        for node in nodes:
            msg = node.get("message")
            if not msg:
                continue

            stats["messages_scanned"] += 1

            msg_id = msg.get("id", node.get("id", f"unknown_{ordinal}"))
            role = (msg.get("author") or {}).get("role", "unknown")
            content_obj = msg.get("content") or {}
            content_type = content_obj.get("content_type", "text")
            weight = msg.get("weight", 1.0)

            # ── Skip internal UI content types ──
            if content_type in SKIP_CONTENT_TYPES:
                stats["skipped_type"] += 1
                continue

            # ── Skip weight-0 system messages with empty content ──
            if role == "system" and weight == 0.0:
                text = _extract_content(msg)
                if not text.strip():
                    stats["skipped_system"] += 1
                    continue

            # ── Extract text content ──
            content = _extract_content(msg)

            # ── Skip empty thinking messages ──
            if content_type in THINKING_CONTENT_TYPES and not content.strip():
                stats["skipped_empty"] += 1
                continue

            # ── Extract tool info ──
            tool_call_id, tool_calls, tool_name = _extract_tool_info(msg, role)

            # ── Handle empty content ──
            if not content.strip() and not tool_calls:
                if role == "user":
                    # Image-only user messages: keep with placeholder
                    attachments = (msg.get("metadata") or {}).get("attachments")
                    if attachments:
                        content = "[Attachment uploaded]"
                    else:
                        stats["skipped_empty"] += 1
                        continue
                elif role == "tool":
                    # Try to get content from search metadata
                    search_source = (msg.get("metadata") or {}).get("search_source")
                    if search_source and isinstance(search_source, dict):
                        content = json.dumps(search_source, ensure_ascii=False)[:32000]
                    else:
                        stats["skipped_empty"] += 1
                        continue
                else:
                    # Empty assistant/system messages with no tool calls — skip
                    stats["skipped_empty"] += 1
                    continue

            # ── Timestamp ──
            timestamp = _parse_timestamp(msg.get("create_time"), fallback_ts)

            # ── Build message for token counting ──
            msg_dict: dict[str, Any] = {"role": role, "content": content}
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
            if tool_name and role == "tool":
                msg_dict["tool_name"] = tool_name

            token_estimate = count_message_tokens(msg_dict)

            candidates.append(ChatGPTImportCandidate(
                source_message_id=msg_id,
                source_conversation_id=conv_id,
                source_session=source_session,
                target_session_id=source,
                source=source,
                role=role,
                content=content,
                tool_call_id=tool_call_id,
                tool_calls=tool_calls,
                tool_name=tool_name,
                timestamp=timestamp,
                token_estimate=token_estimate,
                ordinal=ordinal,
            ))
            ordinal += 1

    stats["conversations"] = len(conversation_ids)
    return candidates, stats


def _load_conversations(source_dir: Path) -> list[dict]:
    """Load all conversations from ChatGPT export JSON files."""
    conversations: list[dict] = []
    json_files = sorted(source_dir.glob("conversations-*.json"))

    if not json_files:
        raise FileNotFoundError(
            f"No conversations-*.json files found in {source_dir}"
        )

    for json_file in json_files:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            conversations.extend(data)
        elif isinstance(data, dict):
            # Single conversation object
            conversations.append(data)

    return conversations


# ── Database helpers ─────────────────────────────────────────────────────────

def _target_has_import_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lcm_imported_messages'"
    ).fetchone()
    return row is not None


def _ensure_import_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lcm_imported_messages (
            import_id TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            source_conversation_id INTEGER NOT NULL,
            source_session TEXT NOT NULL,
            target_store_id INTEGER NOT NULL,
            imported_at REAL NOT NULL,
            PRIMARY KEY (import_id, source_message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lcm_imported_messages_target
            ON lcm_imported_messages(target_store_id)
        """
    )


def _existing_source_ids(target_db: Path, import_id: str) -> set[int]:
    """Check which source message IDs (as integer hashes) have already been imported."""
    if not target_db.exists():
        return set()
    conn = sqlite3.connect(str(target_db))
    try:
        if not _target_has_import_table(conn):
            return set()
        rows = conn.execute(
            "SELECT source_message_id FROM lcm_imported_messages WHERE import_id = ?",
            (import_id,),
        ).fetchall()
        return {int(row[0]) for row in rows}
    finally:
        conn.close()


def _backup_target(target_db: Path) -> str | None:
    """Create a timestamped backup of the target DB."""
    if not target_db.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = target_db.with_name(f"{target_db.name}.backup-{stamp}")
    suffix = 1
    while backup_path.exists():
        backup_path = target_db.with_name(f"{target_db.name}.backup-{stamp}-{suffix}")
        suffix += 1

    source_conn = sqlite3.connect(target_db.resolve().as_uri() + "?mode=ro", uri=True)
    backup_conn = sqlite3.connect(backup_path)
    try:
        source_conn.backup(backup_conn)
    finally:
        backup_conn.close()
        source_conn.close()
    return str(backup_path)


# ── Main import function ────────────────────────────────────────────────────

def import_chatgpt_export(
    *,
    source_dir: str | Path,
    target_db: str | Path,
    namespace: str = "chatgpt-export",
    import_id: str | None = None,
    apply: bool = False,
) -> ImportResult:
    """Import ChatGPT conversations from export JSON into Hermes LCM."""
    source_path = Path(source_dir)
    target_path = Path(target_db)

    if not source_path.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source_path}")

    resolved_import_id = import_id or _default_import_id(source_path)

    # ── Load and parse conversations ──
    conversations = _load_conversations(source_path)
    candidates, stats = _collect_candidates(conversations, namespace=namespace)

    # ── Build integer hash map for tracking (conv_id + msg_id → int) ──
    # ChatGPT reuses some message IDs across conversations (e.g. shared system
    # prompts), so we key on (conversation_id, message_id) to guarantee uniqueness.
    source_id_map: dict[tuple[str, str], int] = {}
    for c in candidates:
        key = (c.source_conversation_id, c.source_message_id)
        if key not in source_id_map:
            source_id_map[key] = _compound_key_to_int(c.source_conversation_id, c.source_message_id)

    # ── Check for already-imported messages ──
    existing_int_ids = _existing_source_ids(target_path, resolved_import_id)
    to_import = [
        c for c in candidates
        if source_id_map[(c.source_conversation_id, c.source_message_id)] not in existing_int_ids
    ]
    skipped_existing = len(candidates) - len(to_import)

    result = ImportResult(
        source_dir=str(source_path),
        target_db=str(target_path),
        import_id=resolved_import_id,
        files_scanned=len(list(source_path.glob("conversations-*.json"))),
        conversations_scanned=stats["conversations"],
        messages_scanned=stats["messages_scanned"],
        eligible=len(candidates),
        would_import=len(to_import) if not apply else 0,
        imported=0,
        skipped_existing=skipped_existing,
        skipped_empty=stats["skipped_empty"],
        skipped_system=stats["skipped_system"],
        skipped_type=stats["skipped_type"],
        conversations_imported=0,
    )

    if not apply:
        result.would_import = len(to_import)
        return result

    if not to_import:
        return result

    # ── Apply mode: backup and write ──
    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_target(target_path)
    result.backup_path = backup_path

    protection_config = LCMConfig.from_env()
    protection_config.database_path = str(target_path)
    store = MessageStore(
        target_path,
        ingest_protection_config=protection_config,
        hermes_home=str(target_path.parent),
    )
    conn = store._conn
    _ensure_import_table(conn)

    imported = 0
    imported_conversations: set[str] = set()

    try:
        for candidate in to_import:
            msg: dict[str, Any] = {
                "role": candidate.role,
                "content": candidate.content,
            }
            if candidate.tool_call_id:
                msg["tool_call_id"] = candidate.tool_call_id
            if candidate.tool_calls:
                msg["tool_calls"] = candidate.tool_calls
            if candidate.tool_name:
                msg["tool_name"] = candidate.tool_name

            protected_msg = protect_message_for_ingest(
                msg,
                config=protection_config,
                hermes_home=str(target_path.parent),
                session_id=candidate.target_session_id,
            )
            tool_calls_json = (
                json.dumps(protected_msg.get("tool_calls"))
                if protected_msg.get("tool_calls")
                else None
            )

            cur = conn.execute(
                """INSERT INTO messages
                   (session_id, source, role, content, tool_call_id, tool_calls,
                    tool_name, timestamp, token_estimate, pinned)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    candidate.target_session_id,
                    _normalize_source_value(candidate.source),
                    protected_msg.get("role", candidate.role),
                    normalize_content_value(protected_msg.get("content")),
                    protected_msg.get("tool_call_id"),
                    tool_calls_json,
                    protected_msg.get("tool_name"),
                    candidate.timestamp,
                    count_message_tokens(protected_msg),
                ),
            )

            # ── Track in import table ──
            int_msg_id = source_id_map[(candidate.source_conversation_id, candidate.source_message_id)]
            int_conv_id = _compound_key_to_int(candidate.source_conversation_id)
            conn.execute(
                """INSERT INTO lcm_imported_messages
                   (import_id, source_message_id, source_conversation_id, source_session,
                    target_store_id, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    resolved_import_id,
                    int_msg_id,
                    int_conv_id,
                    candidate.source_session,
                    int(cur.lastrowid),
                    time.time(),
                ),
            )
            imported += 1
            imported_conversations.add(candidate.source_conversation_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        store.close()

    result.imported = imported
    result.would_import = 0
    result.conversations_imported = len(imported_conversations)
    return result


# ── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import ChatGPT export conversations into Hermes LCM.",
    )
    parser.add_argument(
        "--source-dir",
        required=True,
        help="Path to the ChatGPT export directory (containing conversations-*.json)",
    )
    parser.add_argument(
        "--target-db",
        required=True,
        help="Path to the target Hermes LCM SQLite DB",
    )
    parser.add_argument(
        "--namespace",
        default="chatgpt-export",
        help="Provenance namespace for imported rows (default: chatgpt-export)",
    )
    parser.add_argument(
        "--import-id",
        help="Stable idempotency key. Defaults to SHA-256 hash of the source directory path",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write rows to the target DB. Without this flag, runs in dry-run mode",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    result = import_chatgpt_export(
        source_dir=args.source_dir,
        target_db=args.target_db,
        namespace=args.namespace,
        import_id=args.import_id,
        apply=args.apply,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"ChatGPT export import [{mode}]")
        print(f"  source_dir:              {result.source_dir}")
        print(f"  target_db:               {result.target_db}")
        print(f"  import_id:               {result.import_id}")
        print(f"  files_scanned:           {result.files_scanned}")
        print(f"  conversations_scanned:   {result.conversations_scanned}")
        print(f"  messages_scanned:        {result.messages_scanned}")
        print(f"  eligible:                {result.eligible}")
        if not args.apply:
            print(f"  would_import:            {result.would_import}")
        else:
            print(f"  imported:                {result.imported}")
        print(f"  skipped_existing:        {result.skipped_existing}")
        print(f"  skipped_empty:           {result.skipped_empty}")
        print(f"  skipped_system:          {result.skipped_system}")
        print(f"  skipped_type:            {result.skipped_type}")
        print(f"  conversations_imported:  {result.conversations_imported}")
        if result.backup_path:
            print(f"  backup_path:             {result.backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())