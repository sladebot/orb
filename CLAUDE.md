# Orb — Development Guidelines

## TUI and Dashboard parity rule

**Always keep TUI (`orb/cli/tui.py`) and dashboard (`web/server.py`, `web/static/app.js`) in sync.**

## Rules
1. For any change or fixes first write test, then make changes, and make sure to pass the test.
2. For dashboard screenshot/debugging, use headless Chrome against the live local server instead of GUI capture. Command:
   `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --hide-scrollbars --window-size=1440,980 --screenshot=/tmp/orb-dashboard.png http://127.0.0.1:8080`
3. Always use config and provider catalog data to select model and provider. Never hardcode model ids, provider choices, or inline fallback defaults in runtime logic.
4. Always run or restart the Orb daemon with host `0.0.0.0`.

Any feature, fix, or behaviour change that affects one must be applied to the other:
- Message type changes (e.g. `MessageType.TASK` → `RESPONSE`)
- New agent callbacks (`_on_activity`, `_on_file_write`, `_on_complete`)
- Conversation carryover / session history
- Model propagation at completion time
- Init event broadcast after bridge setup
- Inject flow changes

When in doubt, grep for the same pattern in both files before shipping.
