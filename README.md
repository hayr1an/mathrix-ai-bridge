# Claude Desktop bridge

A local service that lets a web app ask **Claude Desktop** a question and get the
answer back — by driving the real macOS app through the accessibility API, the
same one VoiceOver uses.

No API key. No hosted model. The answers come from the Claude Desktop session
already running on the machine, with its own context, its own tools and its own
files.

```
your app ──► HTTP :8765 ──► queue ──► worker ──► macOS AX ──► Claude Desktop
    ▲                                                              │
    └───────── answer, read back via the message's Copy button ────┘
```

Built for [Mathrix AI](https://github.com/hayr1an/mathrix-ai), a Berlin
out-of-home advertising platform, where it powers an assistant panel that
answers questions about the inventory on screen.

---

## Why this is harder than it sounds

Automating an Electron app through accessibility is less like calling a service
and more like operating a machine that is being rebuilt while you touch it. Four
properties of the app shape every design decision here — each was found by
driving the live app, not by reading documentation.

### Every lookup is scoped by an ancestor, never by name alone

The transcript, the sidebar and the composer all live in one tree. Search it for
a button called `Copy` and you get one from every message; search for an edit
control and you get the sidebar's search box as well as the prompt box. So the
first primitive is *"is this node inside that one?"*, and everything is built on
it.

### Nothing trusts a return value

`AXPress` returns success for controls that do nothing at all. And a press that
*does* work can destroy the element it was called on, surfacing as an error for
an action that succeeded. So each action tries a ladder of routes —
press, alternate action, focus-then-press, real click — re-resolving the control
in a fresh snapshot each time, and decides by re-checking the **goal**: is the
panel gone, did the clipboard change, is the right conversation on screen now.

### The tree goes to sleep

Chromium does not build an accessibility tree until something asks, and lets it
lapse afterwards. A dormant window still answers — with a few dozen nodes of
window chrome, which reads exactly like an app with no UI.

Measured live: `AXWindows` returned two copies of the *application itself* until
`AXManualAccessibility` was set, then three real windows a second later. So
"awake" is a node count, not a successful call — and it is never latched, because
a tree that sleeps again has to be re-woken.

### The app virtualises its lists

Only what is near the viewport exists as nodes. A live read found three message
groups whose last one was `Message 8`. So counting messages cannot answer *"has a
new reply arrived?"* — one message can scroll out as another scrolls in and leave
the count identical. Message **ordinals** are absolute, and everything that
compares across time uses those.

The same applies to the sidebar, which is why a conversation must be scrolled
into view to switch to it. The app's own search modal would be the obvious way
around that, and is not: it publishes its Close button to the accessibility tree
and nothing else — no result rows, even with "10 results available" on screen.

## Layout

```
bridge/
  core.py       Node / Snapshot, and every rule for reading the app — no platform calls
  mac_ax.py     the PyObjC layer: one wrapper per system call, no logic
  macos.py      walking the live tree, wake(), the actions, the waiting
  jobs.py       the file-backed queue and its single-worker lock
  service.py    the localhost JSON API and the worker loop
  inspect.py    tree dumps — the tool you live in when something changes
tests/          49 tests against hand-built trees; no Mac required
examples/nextjs/  the web side: server-side proxy, polling store, chat UI
```

The split is the point. `core.py` holds most of the logic and never touches a
platform handle, so the whole interpretation layer — is it generating, which
message is Claude's, what the question panel is asking, which sidebar row is
which — is tested against hand-built trees with no Mac in the loop. `mac_ax.py`
is the only file that would be rewritten for another OS.

## Running it

Needs macOS, Python 3.11+, and Claude Desktop open with a conversation on screen.

```bash
./start-assistant.sh      # builds .venv-bridge on first run, then serves :8765
```

**Accessibility permission** is granted to the app that *launches* the bridge —
Terminal, iTerm, VS Code — not to Python. System Settings → Privacy & Security →
Accessibility, then restart that app: the permission is only read at launch.

Set `thread_title` in `bridge/config.json` (copy `config.example.json`) to the
conversation you want driven. Its title must be unique — the driver refuses to
choose between two chats of the same name rather than silently sending to
whichever happens to be showing.

### The API

| | |
|---|---|
| `GET /api/health` | is the app up, is the tree awake, which conversation is open |
| `GET /api/chats` | conversations currently reachable in the sidebar |
| `POST /api/messages` | queue a prompt; returns a job |
| `GET /api/messages/{id}` | poll it — `queued → running → done \| error`, with partial text while it streams |
| `POST /api/messages/{id}/answer` | answer a question Claude asked back |

## Security

The bridge has no authentication and drives a real Claude session, so it binds
`127.0.0.1` only and nothing should expose it further. In the example
integration the browser never talks to it: a server-side route is the only
client and forwards an explicit allowlist of paths, refuses requests whose
`Host` is not loopback, and rate-limits sending.

## Development

```bash
.venv-bridge/bin/python -m pytest tests/ -q          # 49 tests, no Mac needed
.venv-bridge/bin/python -m bridge.inspect            # dump the accessibility tree
.venv-bridge/bin/python -m bridge.inspect --grep prompt --context 5
.venv-bridge/bin/python -m bridge.inspect --watch    # print what changes as you click
```

Every control name in `core.py` was read off a live dump rather than guessed.
Guessing is what breaks this kind of driver, repeatedly — capture the shape
first, then write the detection. A Claude Desktop update can rename a control;
when it does, the failure is a lookup finding nothing, and the fix is to dump the
tree and change the constant, not to loosen the matching.

## Known limits

- **macOS only.** The driver contract is portable; `mac_ax.py` is not.
- **Switching conversations needs the sidebar row on screen** (see virtualisation
  above). A conversation already open is sent to without needing any row.
- **No recovery from a tree that sleeps mid-turn.** `wake()` recovers one that is
  dormant when a job starts; a tree that lapses while an answer is streaming
  fails that job.

## Licence

All rights reserved — see [LICENSE](LICENSE). Published so you can read it, not
so you can use it. If you want to use any of it, ask.
