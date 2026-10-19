# Mission Control polish acceptance checks

Run `EMC_DEBUG=1 .venv/bin/python main.py` from the project root after quitting
any other EMC instance. Use disposable documents for close tests.

| Scenario | Expected result |
| --- | --- |
| Close and minimize a desktop window | Target raised before action; no artificial pause |
| Enter from fullscreen, queue desktop actions, exit | Correct desktop window acts; bounded activation fallback if inaccessible |
| Two same-title windows | Only selected window acts; unresolved identity refuses the action |
| Rename a window after queuing | Captured window remains the target |
| Close a document with unsaved edits | Save prompt remains; no repeated close or automatic dismissal |
| Xcode welcome / Preview Open dialog | Unsupported Close/Minimize controls absent |
| Open a dialog without switching apps | Capability refresh catches it before the next settled session |
| Queue, cancel, then queue another action | Canceled identity/retry is removed; latest action wins |
| Rapid clicks and re-enter Mission Control | No queued click applies to a reassigned button; deferred work waits for exit |
| Multiple displays / Spaces | Desktop controls stay on their windows; fullscreen previews have no EMC controls |

`ACTION-TIMING` logs separate time spent queued (including the user's time in
Mission Control) from dispatch cost. `ACTION-VERIFIED` logs observed completion
for retained close/minimize targets. Missing verification is not proof of
failure: a save prompt or inaccessible AX element may need user attention.

Automated coverage (no mouse input):

```
.venv/bin/python scripts/test_action_identity.py
.venv/bin/python scripts/test_deferred_spaces.py
.venv/bin/python scripts/test_input_dispatch.py
.venv/bin/python scripts/test_exit_latch.py
.venv/bin/python scripts/test_zoom_suppression.py
.venv/bin/python scripts/test_untitled_filter.py
```

Live checks above remain necessary; mocked tests cannot establish macOS visual
behavior or end-to-end latency. AX-opaque windows still use legacy fallbacks;
retained identity and completion verification currently cover close/minimize.
