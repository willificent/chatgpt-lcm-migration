# ChatGPT LCM Migration

A production-grade script for importing ChatGPT data exports into the Hermes LCM (Lossless Context Management) database. Handles ChatGPT's tree-structured conversations, deduplicates across imports, and preserves full message metadata including tool calls, code execution, and image references.

## Why This Exists

ChatGPT data exports contain months or years of conversation history in a tree format that doesn't map trivially to a flat message store. This script:

- **Walks ChatGPT's branching conversation trees** to extract the active conversation path (handling edits, regenerations, and branched replies)
- **Filters out UI artifacts** (browsing displays, quote suggestions) and empty content (thinking blocks with no text)
- **Produces a clean, searchable corpus** in the Hermes LCM SQLite database with full provenance tracking
- **Is fully idempotent** — re-running the same import skips all previously imported messages

## Quick Start

```bash
# 1. Export your ChatGPT data (Settings → Data Controls → Export Data)
#    This gives you a conversations.json or conversations-*.json files

# 2. Dry-run to preview what would be imported
python3 import_chatgpt_export.py \
  --source-dir /path/to/chatgpt-export \
  --target-db ~/.hermes/lcm.db

# 3. Apply the import (creates automatic backup)
python3 import_chatgpt_export.py \
  --source-dir /path/to/chatgpt-export \
  --target-db ~/.hermes/lcm.db \
  --apply
```

## Requirements

- **Python 3.10+**
- **Hermes LCM plugin** installed (`hermes-lcm` package) — provides `hermes_lcm.config`, `hermes_lcm.store`, `hermes_lcm.ingest_protection`, `hermes_lcm.message_content`, and `hermes_lcm.tokens`
- The target database must be a valid Hermes LCM SQLite database (the script will create the tracking table if it doesn't exist)

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--source-dir` | Path to ChatGPT export directory (contains `conversations-*.json`) | Required |
| `--target-db` | Path to Hermes LCM SQLite database | `~/.hermes/lcm.db` |
| `--namespace` | Session namespace prefix | `chatgpt-export` |
| `--import-id` | Unique identifier for this import batch | Auto-derived from source dir |
| `--apply` | Actually write to the database (default is dry-run) | Off |
| `--json` | Output results as JSON | Off |

## How It Works

### 1. Conversation Tree Walking

ChatGPT stores conversations as trees, not lists. Each conversation has a `mapping` of message nodes, where each node has a `parent` reference and `children` array. The `current_node` field points to the latest active leaf.

The script walks from `current_node` back to the root, collecting the **active branch** — the path the user actually saw. This correctly handles:

- **Edited messages** (user edited a prompt → only the edited version is kept)
- **Regenerated responses** (user hit regenerate → only the active response is kept)
- **Branching conversations** (user tried multiple approaches → only the selected branch is kept)

### 2. Content Extraction

ChatGPT message content can be:
- A simple string (`"Hello"`)
- A list of content parts (text, images, code execution results)
- Empty or null

The `_extract_content()` function handles all cases, including:
- Image references (preserves the `content_type` metadata)
- Code execution inputs and outputs
- Multi-part messages

### 3. Filtering

The following are excluded from import:

| Filter | What it catches | Why |
|--------|----------------|-----|
| **Empty content** | Thinking blocks with no text, null messages | No retrievable content |
| **System prompts (weight=0)** | ChatGPT's internal system instructions (e.g., "You are ChatGPT...") | Not user-authored content; adds noise |
| **UI content types** | `tether_browsing_display`, `tether_quote` | Browser UI artifacts, not conversation content |
| **Empty tool results** | Tool responses with no content | No retrievable content |

### 4. Session Namespacing

Each conversation is imported under the session ID:

```
chatgpt-export:conversation:{conversation_uuid}
```

This keeps ChatGPT conversations separate from other LCM sessions (Hermes native, OpenClaw imports, etc.) while making them fully searchable via `lcm_grep`.

### 5. Idempotent Import Tracking

The script uses a `lcm_imported_messages` tracking table with:

- **import_id**: Identifies the import batch (default: hash of source directory path)
- **source_message_id**: Integer hash of `(conversation_id, message_id)` — see [Issues & Solutions](#issues-encountered--solutions) for why this is compound
- **source_conversation_id**: Integer hash of the conversation UUID
- **target_store_id**: The LCM store_id assigned to the imported message

Re-running with the same `import_id` skips all previously imported messages. You can use `--import-id` to force a fresh import from the same source.

## Issues Encountered & Solutions

### Issue 1: ChatGPT Message UUIDs Are Not Globally Unique

**Problem:** ChatGPT reuses message IDs across conversations. Our export contained 111 message UUIDs that appeared in 2–4 different conversations each (shared system prompts like "You are ChatGPT, a large language model..."). Using the message UUID alone as a tracking key caused `UNIQUE constraint` violations:

```
sqlite3.IntegrityError: UNIQUE constraint failed: lcm_imported_messages.source_message_id
```

**Root cause:** The original `_uuid_to_int()` function hashed only the message UUID. When the same UUID appeared in multiple conversations, the hash collided.

**Solution:** Changed from `_uuid_to_int(uuid_str)` to `_compound_key_to_int(*parts)` which hashes `(conversation_id, message_id)` together using a null-byte separator. This guarantees uniqueness even when message UUIDs are shared:

```python
def _compound_key_to_int(*parts: str) -> int:
    compound = "\x00".join(parts)
    digest = hashlib.sha256(compound.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**63 - 1)
```

The tracking dict key also changed from `dict[str, int]` (message_id → hash) to `dict[tuple[str, str], int]` ((conversation_id, message_id) → hash) to prevent collisions at the Python level before database insertion.

### Issue 2: ChatGPT Conversation Trees Have Multiple Active Paths

**Problem:** The initial naive approach of iterating all nodes in `mapping` produced 9,906 messages, but many were from inactive branches (previous edits, abandoned regenerations). The conversation tree structure means the same "slot" in a conversation can have multiple messages (the original, the edit, the regeneration).

**Solution:** Implemented `_walk_conversation()` which:
1. Starts at `current_node` (the active branch tip)
2. Walks backward via `parent` references to the root
3. Returns only the nodes on this active path

This reduced the eligible messages from 9,906 to 4,742 — removing both inactive branches and internal system content.

### Issue 3: System Prompts Vary in Weight

**Problem:** Not all system messages should be equally treated. ChatGPT injects weight-0 system prompts that are internal instructions ("You are ChatGPT...", browsing tool descriptions, etc.) rather than meaningful conversation content.

**Solution:** Filter out system messages with `metadata.weight == 0` or missing weight. This preserves user-set custom instructions (weight > 0) while removing internal boilerplate. In our export, this filtered out **2,151 system prompts** — nearly half the total messages.

### Issue 4: Timestamp Format Inconsistency

**Problem:** ChatGPT exports use multiple timestamp formats across different export versions:
- Unix seconds (float or int)
- ISO 8601 with timezone (`2024-01-15T10:30:00.000Z`)
- ISO 8601 without timezone
- Occasionally missing timestamps

**Solution:** `_parse_timestamp()` tries formats in order: Unix float → ISO with tz → ISO without tz → fallback to 0 (beginning of epoch). Messages with missing timestamps get `0.0` and are still imported (just not chronologically ordered within their conversation).

### Issue 5: Empty Thinking/Reasoning Messages

**Problem:** ChatGPT's "thinking" and "reasoning_recap" content types often contain empty strings or only whitespace. These are internal chain-of-thought that ChatGPT generates but doesn't display to the user.

**Solution:** After extracting content, filter messages where the resulting text is empty or whitespace-only. Content types `thoughts` and `reasoning_recap` are flagged and skipped when empty. This removed **2,867 empty messages** from our import.

## Import Statistics (Our Export)

| Metric | Count |
|--------|-------|
| Conversations scanned | 303 |
| Total message nodes in trees | 9,906 |
| Eligible for import | 4,742 |
| Actually imported | 4,742 |
| Skipped (empty content) | 2,867 |
| Skipped (system weight=0) | 2,151 |
| Skipped (UI content types) | 146 |
| Date range | Sep 23 2025 → Mar 10 2026 (~6 months) |
| Longest conversation | 328 messages (LSAT explanations) |
| Conversations with 50+ messages | 55 |

## Output Format

Each imported message is stored in the Hermes LCM `messages` table with:

```
session_id:    chatgpt-export:conversation:{uuid}
source:        chatgpt-export:conversation:{uuid}
role:          user | assistant | tool
content:       [extracted text content]
timestamp:     [Unix epoch seconds]
token_estimate:[estimated token count]
```

Tool call metadata, image references, and code execution results are preserved in the message content as structured JSON alongside the text content.

## Re-running / Updating

The script is fully idempotent:

```bash
# This always shows 0 would-import after a successful apply
python3 import_chatgpt_export.py \
  --source-dir /path/to/chatgpt-export \
  --target-db ~/.hermes/lcm.db

# Output:
#   would_import: 0
#   skipped_existing: 4742
```

For a fresh import from the same source (e.g., after a database reset):

```bash
python3 import_chatgpt_export.py \
  --source-dir /path/to/chatgpt-export \
  --target-db ~/.hermes/lcm.db \
  --import-id fresh-2026-05-14 \
  --apply
```

## Database Safety

- **Dry-run mode** (default) makes no modifications — always run this first
- **`--apply` mode** creates a timestamped backup at `{target_db}.backup-{YYYYMMDDHHMMSS}` before any writes
- All insertions happen within a single database transaction — if anything fails, the entire import is rolled back
- The tracking table prevents duplicate imports even if the backup-rollback fails

## License

MIT — because your conversation data is yours, and the tools to manage it should be too.

## Acknowledgments

Built for the [Hermes Agent](https://hermes-agent.nousresearch.com/) LCM system. The `import_lossless_claw.py` script from `hermes-lcm` served as the architectural template (session namespacing, tracking table pattern, backup-before-write, dry-run-first CLI).