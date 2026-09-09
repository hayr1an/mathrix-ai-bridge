# The website's assistant, and the bridge behind it

The chat panel on the Mathrix AI site does not call a hosted model. It drives
**Claude Desktop on this Mac** through the macOS accessibility API, reads the
answer back out of the transcript, and returns it to the browser.

Built from `DRIVERSPEC.md`. Every control name in `detect.py` was read off a live
tree dump rather than guessed — see §6 of the spec for why that matters.

```
browser ──► Next.js /api/assistant/* ──► bridge :8765 ──► worker ──► macOS AX ──► Claude Desktop
   ▲                (server-side)          (queue)                                     │
   └──────────────────── answer, read back via the message's Copy button ──────────────┘
```

## Running it

```bash
./start-assistant.sh      # from the Poster/ directory; leave it running
npm run dev               # in web/
```

The first run builds `.venv-bridge` from `bridge/requirements.txt`. Requires
Python 3.11+ and Claude Desktop open with a conversation on screen.

**Accessibility permission** is granted to the app that *launches* the bridge —
Terminal, iTerm or VS Code — not to Python. System Settings → Privacy & Security
→ Accessibility, then restart that app: the permission is only read at launch.

Pick which Claude Desktop conversation the site uses from the gear icon in the
chat panel. It must be a conversation visible in the sidebar (open it once by
hand), and its title must be unique — the driver refuses to guess between two
chats of the same name rather than silently sending to whichever is showing.

## The pieces

| file | what it is |
|---|---|
| `tree.py` | `Node` / `Snapshot` and the flat-tree arithmetic. No platform calls. |
| `detect.py` | All interpretation: is it generating, which message is Claude's, the question panel, the sidebar. Pure — no platform calls. |
| `mac_ax.py` | The PyObjC layer. One wrapper per system call, no logic. |
| `walk.py` | Flattening the live tree, and `wake()`. |
| `driver.py` | The actions, the activation ladder, and the waiting. |
| `jobs.py` | The file-backed queue shared by server and worker. |
| `worker.py` | One job at a time, driving the app. |
| `server.py` | The localhost JSON API. |
| `inspect.py` | Tree dumps. The tool you live in when something changes. |

`tree.py` and `detect.py` are where most of the logic is, and neither needs a Mac
to test: `pytest tests/` runs against hand-built trees.

## When something breaks

```bash
.venv-bridge/bin/python -m bridge.inspect                  # dump the tree
.venv-bridge/bin/python -m bridge.inspect --grep prompt    # find one control
.venv-bridge/bin/python -m bridge.inspect --roles          # raw AX roles
.venv-bridge/bin/python -m bridge.inspect --watch          # what changes as you click
.venv-bridge/bin/python -m pytest tests/ -q
```

A Claude Desktop update can rename a control. When it does, the failure is a
lookup returning nothing, and the fix is to dump the tree, find the new name,
and change the constant at the top of `detect.py` — not to loosen the matching.

### Things that are deliberate

* **`wake()` never latches.** A tree that goes dormant later has to be
  re-wakeable, and a stub tree raises an error naming *accessibility* rather
  than letting every later lookup fail as "conversation not found".
* **Every lookup is scoped by ancestor.** The transcript, the sidebar and the
  composer are all in one tree; an unscoped search for a `Copy` button finds one
  in every message.
* **Setting the composer's value, not typing.** Synthesised keys go to whatever
  window has focus — and with a browser in front, `Cmd+A` `Cmd+V` would
  select-all-and-replace *in the browser*. Typing is the fallback.
* **Actions verify the goal, not the return value.** `AXPress` returns success
  for controls that do nothing, and a press that works can destroy the element
  and surface as an error. So each route is tried, then the goal is re-checked.
* **The transcript must grow before an answer is read.** Otherwise a failed send
  reads the previous answer back and the job looks like it worked.

## Configuration

`bridge/config.json` — the timings are meant to be retuned. If `wake()` is
flaky on your machine, raise `settle_seconds` first.

| key | default | |
|---|---|---|
| `thread_title` | `""` | The conversation to type into. Set from the UI. |
| `settle_seconds` | `2.5` | Pause after poking the tree before reading it. |
| `wake_timeout` | `30` | How long to keep trying to wake a dormant tree. |
| `generation_timeout` | `900` | Cap on one answer, end to end. |
| `idle_grace` | `3.0` | Generation must read false this long to count as done. |
| `walk_depth` | `80` | The composer sits ~40 levels down; a shallow walk silently truncates. |
| `system_preamble` | `""` | Prepended to every prompt from the website. |

## Security

The bridge has **no authentication** and drives a real Claude session, so it
binds `127.0.0.1` and the browser never talks to it directly. The Next.js route
at `web/src/app/api/assistant/[...path]/route.ts` is the only client, it runs
server-side, and it forwards an explicit allowlist of paths — anything else is a
404. Do not change the bind address without putting auth in front of it.
