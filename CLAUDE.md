# Orb — Development Guidelines

## TUI and Dashboard parity rule

**Always keep TUI (`orb/cli/tui.py`) and dashboard (`web/server.py`, `web/static/app.js`) in sync.**

## Rules
1. For any change or fixes first write test, then make changes, and make sure to pass the test.

Any feature, fix, or behaviour change that affects one must be applied to the other:
- Message type changes (e.g. `MessageType.TASK` → `RESPONSE`)
- New agent callbacks (`_on_activity`, `_on_file_write`, `_on_complete`)
- Conversation carryover / session history
- Model propagation at completion time
- Init event broadcast after bridge setup
- Inject flow changes

When in doubt, grep for the same pattern in both files before shipping.

## No hardcoded models

Never hardcode model IDs (e.g. `"claude-opus-4-6"`, `"gpt-5.4"`, `"o3"`) anywhere in code. Model IDs must come from the existing config maps in `orb/llm/types.py` (`DEFAULT_MODELS`, `OPENAI_MODELS`, `CODEX_MODELS`) or from user-supplied configuration. When resolving a provider to a model, look it up from these maps at runtime rather than embedding string literals.

