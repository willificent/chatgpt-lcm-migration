# ChatGPT Export Format Notes

This document describes the structure of ChatGPT data exports for anyone looking to parse them independently.

## File Structure

The export directory contains:
- `conversations.json` (or `conversations-*.json` when split by size)
- `user.json` — account details
- `chatgpt-models.json` — model metadata
- Various asset directories (image uploads, etc.)

## Conversation Object

```json
{
  "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "title": "LSAT Practice Questions",
  "create_time": 1705312800.0,
  "update_time": 1705399200.0,
  "mapping": {
    "node-id-1": {
      "id": "node-id-1",
      "message": {
        "id": "msg-uuid-1",
        "author": { "role": "user" },
        "content": { "content_type": "text", "parts": ["Hello!"] },
        "metadata": {}
      },
      "parent": "client-created-root",
      "children": ["node-id-2"]
    },
    "node-id-2": {
      "id": "node-id-2",
      "message": {
        "id": "msg-uuid-2",
        "author": { "role": "assistant" },
        "content": { "content_type": "text", "parts": ["Hi there!"] },
        "metadata": { "model_slug": "gpt-4" }
      },
      "parent": "node-id-1",
      "children": ["node-id-3"]
    }
  },
  "current_node": "node-id-2",
  "conversation_template_id": "...",
  "gizmo_id": null
}
```

## Key Fields

### `mapping`
A dictionary of node IDs to node objects. Each node represents one message position in the conversation tree. The tree structure allows for:
- **Linear conversations**: single parent → single child chain
- **Edits**: a parent node has multiple children where the user edited their prompt
- **Regenerations**: a parent node has multiple children where the user regenerated the response
- **Branching**: user tried different approaches from the same point

### `current_node`
Points to the active leaf node — the last message in the conversation path the user actually viewed.

### `parent` / `children`
Each node has a `parent` reference and a `children` array. Root nodes have `parent: "client-created-root"` or `parent: null`.

### `message`
Can be `null` for root nodes (which serve as conversation anchors, not actual messages). When present:

| Field | Type | Notes |
|-------|------|-------|
| `id` | string (UUID) | **NOT globally unique** — shared system prompts reuse IDs across conversations |
| `author.role` | string | `user`, `assistant`, `system`, `tool` |
| `content.content_type` | string | `text`, `code`, ` Thoughts`, `tether_browsing_display`, `tether_quote`, etc. |
| `content.parts` | array | Mixed: strings, nulls, image references, execution results |
| `metadata` | object | See metadata notes below |
| `metadata.weight` | number | System prompts: `0` = internal, `1` = user-set |
| `metadata.model_slug` | string | e.g. `gpt-4`, `gpt-3.5-turbo` |

### Message Content Types

| Content Type | Description | Import? |
|--------------|-------------|---------|
| `text` | Regular text message | ✅ Yes |
| `code` | Code blocks | ✅ Yes |
| `thoughts` | Chain-of-thought (internal) | Skip if empty |
| `reasoning_recap` | Reasoning summary | Skip if empty |
| `tether_browsing_display` | Browser UI snapshot | ❌ No — UI artifact |
| `tether_quote` | Selected quote suggestion | ❌ No — UI artifact |
| `execution_output` | Code execution result | ✅ Yes (as tool message) |

### Parts Array

Each part can be:
- `string` — regular text
- `null` — empty (skip)
- `{"content_type": "image_asset_pointer", ...}` — image reference (preserve metadata)
- `{"content_type": "execution_output", ...}` — code execution result

## Timestamp Formats

ChatGPT exports use multiple timestamp formats depending on when the export was generated:

1. **Unix seconds** (float): `1705312800.0` — most common
2. **ISO 8601 with timezone**: `"2024-01-15T10:30:00.000Z"`
3. **ISO 8601 without timezone**: `"2024-01-15T10:30:00"`
4. **Missing/null**: Some nodes have no timestamp

Always try parsing in order: Unix → ISO with tz → ISO without tz → fallback to 0.

## Walk Algorithm

To reconstruct the conversation as the user saw it:

```python
def walk_conversation(mapping, current_node):
    # 1. Start from current_node
    # 2. Walk backward via parent references
    # 3. Collect all non-null message nodes
    # 4. Reverse to get chronological order

    path = []
    node_id = current_node
    visited = set()

    while node_id and node_id in mapping and node_id not in visited:
        visited.add(node_id)
        path.append(node_id)
        parent = mapping[node_id].get("parent")
        if parent is None or parent == "client-created-root":
            break
        node_id = parent

    path.reverse()
    return [mapping[nid] for nid in path if mapping[nid].get("message")]
```

This produces a linear conversation from the tree structure, honoring the user's active branch at each fork point.