"""The pure layer: the flattened accessibility tree, and reading meaning from it.

Nothing in this module touches the platform. `Node.ctrl` is carried through
untouched and never read here, which is what lets every interpretation rule be
tested against hand-built trees with no Mac in the loop - see tests/.

Two halves:

* **The tree.** A depth-first flattening, plus the arithmetic that makes scoping
  cheap. The one idea worth internalising (DRIVERSPEC §0): *every lookup is
  scoped by ancestor, never by name alone*. The transcript, the sidebar and the
  composer all live in one tree, so a bare search for an edit control or a
  button named "Copy" finds several and picks the wrong one.

* **The reading.** Is it generating, which message is Claude's, what the
  question panel is asking, which sidebar row is which. Every control name below
  was read off a live dump (`python -m bridge.inspect`), not guessed - guessing
  is what breaks this kind of driver repeatedly (DRIVERSPEC §6).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional


# ── the tree ──────────────────────────────────────────────────────────────────

# Control types, normalised to the Windows UIA vocabulary. The macOS driver maps
# AXStaticText -> TextControl and so on, so this layer and its tests never learn
# there are two platforms (DRIVERSPEC §5.4).
GROUP = "GroupControl"
TEXT = "TextControl"
BUTTON = "ButtonControl"
EDIT = "EditControl"
LIST = "ListControl"
LIST_ITEM = "ListItemControl"
WINDOW = "WindowControl"
IMAGE = "ImageControl"
PANE = "PaneControl"
UNKNOWN = "UnknownControl"


@dataclass
class Node:
    """One element of the flattened tree.

    `index` is the position in the flat list and `depth` the nesting level; the
    pair is what turns "everything inside this node" into arithmetic instead of
    a traversal.
    """

    index: int
    depth: int
    type: str
    name: str
    ctrl: Any = None
    parent: Optional["Node"] = None
    # Extra platform detail the driver may want later (AX actions, value,
    # position). Never read by the interpretation layer.
    extra: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Node({}, d={}, {}, {!r})".format(
            self.index, self.depth, self.type, self.name
        )

    @property
    def label(self) -> str:
        """The name with surrounding whitespace gone - what every comparison uses."""
        return (self.name or "").strip()

    def ancestors(self) -> Iterator["Node"]:
        """Parents, nearest first. Walks the `parent` links, so it is O(depth)."""
        seen = 0
        node = self.parent
        # The guard is paranoia about a cycle from a malformed tree: a loop here
        # would hang the worker rather than fail it.
        while node is not None and seen < 256:
            yield node
            node = node.parent
            seen += 1

    def under(self, name: str, type: Optional[str] = None) -> bool:
        """Is this node inside a node named `name` (optionally of `type`)?

        Case-insensitive and trimmed, because accessible names pick up stray
        whitespace. This is the scoping primitive from DRIVERSPEC §0.
        """
        wanted = (name or "").strip().casefold()
        for a in self.ancestors():
            if type is not None and a.type != type:
                continue
            if a.label.casefold() == wanted:
                return True
        return False

    def block_ancestor(self) -> Optional["Node"]:
        """The nearest ancestor that is not itself a text control.

        Inline spans - code, links, emphasis - are text nodes nested in text
        nodes. Treating each as its own block put a newline around every
        `identifier` in a sentence (DRIVERSPEC §3). Line breaks key on this
        instead.
        """
        a = self.parent
        while a is not None and a.type == TEXT:
            a = a.parent
        return a


class Snapshot:
    """One depth-first, pre-order flattening of the tree, plus lookups over it.

    Cheap to build and treated as immutable: the app redraws constantly, so a
    Snapshot is a photograph, not a handle. Anything that acts on the app
    re-snapshots first (DRIVERSPEC §4, "references go stale").
    """

    def __init__(self, nodes: Iterable[Node], *, taken_at: float = 0.0):
        self.nodes: list[Node] = list(nodes)
        self.taken_at = taken_at

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[Node]:
        return iter(self.nodes)

    # ── subtree arithmetic ───────────────────────────────────────────────────

    def subtree_end(self, node: Node) -> int:
        """Index of the last node inside `node` (itself, if it has no children).

        A node's subtree runs from its index until the first later node whose
        depth is <= its own. This single operation powers "all the text in this
        message", "the buttons in this panel" and "is this under the
        transcript" (DRIVERSPEC §1).
        """
        end = node.index
        for other in self.nodes[node.index + 1 :]:
            if other.depth <= node.depth:
                break
            end = other.index
        return end

    def subtree(self, node: Node, *, inclusive: bool = False) -> list[Node]:
        start = node.index if inclusive else node.index + 1
        return self.nodes[start : self.subtree_end(node) + 1]

    def contains(self, ancestor: Node, node: Node) -> bool:
        """Index-range containment - the cheap form of `node.under(...)` when the
        ancestor is already in hand."""
        return ancestor.index <= node.index <= self.subtree_end(ancestor)

    # ── lookups ──────────────────────────────────────────────────────────────

    def find_all(
        self,
        *,
        type: Optional[str] = None,
        name: Optional[str] = None,
        prefix: Optional[str] = None,
        contains: Optional[str] = None,
        under: Optional[Node] = None,
        where: Optional[Callable[[Node], bool]] = None,
    ) -> list[Node]:
        """Every node matching all of the given criteria, in tree order.

        String matching is trimmed and case-insensitive throughout: accessible
        names differ in case and padding between builds, and a case-sensitive
        miss reads as "the control does not exist".
        """
        lo, hi = 0, len(self.nodes) - 1
        if under is not None:
            lo, hi = under.index + 1, self.subtree_end(under)

        want_name = name.strip().casefold() if name is not None else None
        want_prefix = prefix.strip().casefold() if prefix is not None else None
        want_sub = contains.strip().casefold() if contains is not None else None

        out: list[Node] = []
        for node in self.nodes[lo : hi + 1]:
            if type is not None and node.type != type:
                continue
            label = node.label.casefold()
            if want_name is not None and label != want_name:
                continue
            if want_prefix is not None and not label.startswith(want_prefix):
                continue
            if want_sub is not None and want_sub not in label:
                continue
            if where is not None and not where(node):
                continue
            out.append(node)
        return out

    def find(self, **kwargs: Any) -> Optional[Node]:
        found = self.find_all(**kwargs)
        return found[0] if found else None

    # ── text ─────────────────────────────────────────────────────────────────

    def labels(self, node: Node, *, inclusive: bool = False) -> list[str]:
        """Non-empty names of the text nodes inside `node`, in reading order."""
        return [
            n.label
            for n in self.subtree(node, inclusive=inclusive)
            if n.type == TEXT and n.label
        ]

    def children(self, node: Node) -> list[Node]:
        """Direct children only - one level down, not the whole subtree."""
        return [n for n in self.subtree(node) if n.depth == node.depth + 1]

    # ── debugging ────────────────────────────────────────────────────────────

    def dump(self, *, max_name: int = 90) -> str:
        lines = []
        for n in self.nodes:
            name = n.label
            if len(name) > max_name:
                name = name[: max_name - 1] + "…"
            lines.append("{:>5} {}{} {!r}".format(n.index, "  " * n.depth, n.type, name))
        return "\n".join(lines)

    def to_dict(self) -> list[dict[str, Any]]:
        """Serialisable form, for `inspect --save`. Fake trees are rebuilt from
        exactly this, which is how a captured session becomes a test."""
        return [
            {"index": n.index, "depth": n.depth, "type": n.type, "name": n.name}
            for n in self.nodes
        ]


def build(rows: Iterable[Any]) -> Snapshot:
    """A Snapshot from `(depth, type, name)` tuples or the dicts `to_dict` emits.

    Parent links are recovered from the depth column with a running stack, so a
    capture on disk and a live walk produce identical trees. Tests build fake
    trees through this.
    """
    nodes: list[Node] = []
    stack: list[Node] = []
    for row in rows:
        if isinstance(row, dict):
            depth, ctype, name = row["depth"], row["type"], row.get("name") or ""
        else:
            depth, ctype, name = row[0], row[1], row[2] if len(row) > 2 else ""
        while stack and stack[-1].depth >= depth:
            stack.pop()
        node = Node(
            index=len(nodes),
            depth=depth,
            type=ctype,
            name=name or "",
            parent=stack[-1] if stack else None,
        )
        nodes.append(node)
        stack.append(node)
    return Snapshot(nodes)


# ── reading the app ───────────────────────────────────────────────────────────

# ── the landmarks ────────────────────────────────────────────────────────────

TRANSCRIPT = "Chat messages"
SIDEBAR = "Sidebar"
PRIMARY_PANE = "Primary pane"
STREAMING = "Currently streaming message"
MESSAGE_ACTIONS = "Message actions"

# The stop control is named differently per surface - "Stop" on Code,
# "Stop response" on Cowork (DRIVERSPEC §3).
STOP_NAMES = ("Stop", "Stop response", "Stop generating", "Stop generation")
SEND_NAMES = ("Send", "Send message", "Send prompt", "Send Message")
COMPOSER_NAMES = ("Prompt", "Write your prompt to Claude", "Send a message",
                  "How can I help you today?", "Reply to Claude")

# Screen-reader announcements that open every message.
YOU_SAID = "you said:"
CLAUDE_SAID = "claude responded:"
ASSISTANT_PREFIXES = (CLAUDE_SAID, "claude said:")

# Transcript furniture that is not part of any answer.
KEYBOARD_HINT = "use the up and down arrow keys to move between messages."

RENAME_SUFFIX = ", rename session"

# Sidebar rows are "<state> <title>". The last two are not a typo: a chat with no
# session state is named after the unread toggle it contains, so an idle chat's
# row really is called "Mark as unread backend_test" (DRIVERSPEC §4).
ROW_STATES = (
    "Awaiting answer ",
    "Mark as unread ",
    "Mark as read ",
    "Running ",
    "Idle ",
    "Error ",
    "Queued ",
)
# Controls that repeat a chat's title without being its row.
ROW_DECOYS = ("More options for ", "Toggle chats for ", "New session in ",
              "Open ", "Pin ", "Rename ")

# "Message 1", "Message 3 of 12" - but never "Message actions", which shares the
# prefix and is a child of every single message.
MESSAGE_RE = re.compile(r"^message\s+(\d+)", re.IGNORECASE)


# ── generating ───────────────────────────────────────────────────────────────


def transcript(snap: Snapshot) -> Optional[Node]:
    return snap.find(type=GROUP, name=TRANSCRIPT)


def is_generating(snap: Snapshot) -> bool:
    """Two independent signals, either sufficient (DRIVERSPEC §3).

    The scope on the second one is the whole point: buttons *inside* messages
    are per-message actions, and an unscoped search for "Stop" would find them.
    """
    if snap.find(type=GROUP, name=STREAMING) is not None:
        return True
    return stop_button(snap) is not None


def stop_button(snap: Snapshot) -> Optional[Node]:
    """The composer's stop control - never one from inside a message."""
    chat = transcript(snap)
    for name in STOP_NAMES:
        for node in snap.find_all(type=BUTTON, name=name):
            if chat is not None and snap.contains(chat, node):
                continue
            return node
    return None


def send_button(snap: Snapshot) -> Optional[Node]:
    """The send control, when the app is showing one.

    While a turn is running the composer shows Stop instead, so None here is a
    normal state, not an error.
    """
    chat = transcript(snap)
    side = snap.find(name=SIDEBAR)
    for name in SEND_NAMES:
        for node in snap.find_all(type=BUTTON, name=name):
            if chat is not None and snap.contains(chat, node):
                continue
            if side is not None and snap.contains(side, node):
                continue
            return node
    return None


# ── what is on screen ────────────────────────────────────────────────────────


def current_thread_title(snap: Snapshot) -> Optional[str]:
    """The open conversation, from the rename button that carries it.

    Not the window title, which does not track the open thread.
    """
    for node in snap.find_all(type=BUTTON):
        label = node.label
        if label.casefold().endswith(RENAME_SUFFIX):
            title = label[: -len(RENAME_SUFFIX)].strip()
            if title:
                return title
    return None


def message_groups(snap: Snapshot) -> list[Node]:
    """The per-message groups, oldest first, in tree order.

    "Message actions" shares the "Message " prefix and appears inside every
    single message, so the match requires a digit after it.
    """
    chat = transcript(snap)
    if chat is None:
        return []
    return [
        n for n in snap.find_all(type=GROUP, under=chat)
        if MESSAGE_RE.match(n.label)
    ]


def message_count(snap: Snapshot) -> int:
    """How many messages are *currently in the tree*.

    Not how many the conversation has. The app virtualises the transcript, so
    only the messages near the viewport exist as nodes - a live read showed 3
    groups whose last one was "Message 8". Use `message_number` for anything
    that has to be compared across time; this is for display only.
    """
    return len(message_groups(snap))


def message_number(group: Node) -> Optional[int]:
    """The ordinal in "Message 8", or None.

    These numbers are absolute and stable: message 8 stays message 8 however the
    transcript is scrolled. That makes them the only safe way to ask "is this a
    message I have not seen before?" - counting cannot answer it, because
    virtualisation lets one message scroll out as another scrolls in and leaves
    the count unchanged.
    """
    match = MESSAGE_RE.match(group.label)
    return int(match.group(1)) if match else None


def highest_message_number(snap: Snapshot) -> int:
    """The largest message ordinal in the tree, or 0 if there are none."""
    numbers = [n for n in (message_number(g) for g in message_groups(snap)) if n is not None]
    return max(numbers) if numbers else 0


def last_answer_number(snap: Snapshot) -> Optional[int]:
    group = last_answer_group(snap)
    return message_number(group) if group is not None else None


def _announcement(snap: Snapshot, group: Node) -> str:
    """The screen-reader line that opens a message, lowercased, or ""."""
    for node in snap.subtree(group):
        if node.type != TEXT:
            continue
        label = node.label.casefold()
        if label.startswith(YOU_SAID) or label.startswith(ASSISTANT_PREFIXES):
            return label
    return ""


def is_assistant_message(snap: Snapshot, group: Node) -> bool:
    return _announcement(snap, group).startswith(ASSISTANT_PREFIXES)


def last_answer_group(snap: Snapshot) -> Optional[Node]:
    """The most recent message Claude wrote.

    Filtering by the announcement is not cosmetic: without it a failed send
    returns the user's own prompt as the answer and the job looks like it
    worked (DRIVERSPEC §3).
    """
    for group in reversed(message_groups(snap)):
        if is_assistant_message(snap, group):
            return group
    return None


def copy_button(snap: Snapshot, group: Node) -> Optional[Node]:
    """The message's own Copy button, from its actions toolbar.

    Scoped to this message: every message has one, so an unscoped search copies
    an arbitrary other message.
    """
    for actions in snap.find_all(type=GROUP, name=MESSAGE_ACTIONS, under=group):
        found = snap.find(type=BUTTON, name="Copy", under=actions)
        if found is not None:
            return found
    return snap.find(type=BUTTON, name="Copy", under=group)


# ── stitching the answer out of text nodes ───────────────────────────────────


def _is_announcement(node: Node) -> bool:
    folded = node.label.casefold()
    return folded.startswith(YOU_SAID) or folded.startswith(ASSISTANT_PREFIXES)


def _is_inline_wrapper(snap: Snapshot, node: Node) -> bool:
    """Is this unnamed group an inline span rather than a paragraph?

    The live app wraps inline code, links and emphasis in an unnamed group
    holding one text node - structurally identical to the unnamed group that
    holds a whole paragraph:

        Group ''                          <- a paragraph: a block
          Text 'That failure was just macOS lacking'
          Group ''                        <- inline code: not a block
            Text 'timeout'
          Text '. Running directly.'

    What separates them is the parent. A span's parent has prose of its own
    directly inside it - the span is interleaved with that prose. A paragraph's
    parent is the message, whose only direct text is the screen-reader
    announcement, which is why that is excluded from the test.

    Getting this wrong is not subtle: treating spans as blocks splits every
    sentence into three lines around every identifier (DRIVERSPEC §3).
    """
    if node.type != GROUP or node.label:
        return False
    parent = node.parent
    if parent is None:
        return False
    return any(
        kid.type == TEXT and kid.label and not _is_announcement(kid)
        for kid in snap.children(parent)
    )


def block_of(snap: Snapshot, node: Node) -> Optional[Node]:
    """The block a text node belongs to - what line breaks key on."""
    owner = node.parent
    while owner is not None and (owner.type == TEXT or _is_inline_wrapper(snap, owner)):
        owner = owner.parent
    return owner


def _skip_text(node: Node) -> bool:
    label = node.label
    if not label:
        return True
    folded = label.casefold()
    # The announcement itself, and the copy of it in a child node.
    if folded.startswith(YOU_SAID) or folded.startswith(ASSISTANT_PREFIXES):
        return True
    if folded == KEYBOARD_HINT:
        return True
    # Relative timestamps ("just now") and the action labels.
    if node.under(MESSAGE_ACTIONS):
        return True
    # Text nested inside identical text. Each announcement is repeated in a
    # child node, and those used to be pasted on top of the answer.
    parent = node.parent
    if parent is not None and parent.type == TEXT and parent.label == label:
        return True
    return False


def read_answer_text(snap: Snapshot, group: Node) -> str:
    """Stitch a message's prose back together - the fallback when Copy fails.

    Harder than it looks, because the transcript carries more than the prose and
    because inline spans (code, links, emphasis) are text nodes nested inside
    text nodes. Treating each as a block split every sentence into three lines
    around every `identifier`, so a newline is emitted only when the *block*
    ancestor changes (DRIVERSPEC §3).
    """
    lines: list[str] = []
    current: list[str] = []
    block: Optional[Node] = None

    for node in snap.subtree(group):
        if node.type != TEXT or _skip_text(node):
            continue
        owner = block_of(snap, node)
        if block is not None and owner is not block:
            if current:
                lines.append(" ".join(current))
            current = []
        block = owner
        current.append(node.label)

    if current:
        lines.append(" ".join(current))

    # Collapse the runs of blank lines that empty blocks leave behind.
    out: list[str] = []
    for line in (l.strip() for l in lines):
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


# ── the question panel ───────────────────────────────────────────────────────


@dataclass
class Question:
    """A question Claude paused to ask, and the ways to answer it."""

    node: Node
    text: str
    options: list[str]
    free_text: Optional[Node] = None
    skip: Optional[Node] = None

    def to_dict(self) -> dict:
        return {"question": self.text, "options": list(self.options),
                "can_type": self.free_text is not None,
                "can_skip": self.skip is not None}


FREE_TEXT_NAMES = ("Something else", "Other", "Type your answer")
SKIP_NAMES = ("Skip", "Dismiss", "Cancel")
PANEL_CHROME = ("Minimize", "Minimise", "Expand", "Collapse")


def pending_question(snap: Snapshot) -> Optional[Question]:
    """The panel Claude puts up when it stops mid-turn to ask something.

    Detection keys on **structure, not labels** (DRIVERSPEC §4): the option text
    is whatever Claude asked, so there is nothing stable to match by name. The
    shape being matched is

        Group '<the question>'              <- the panel; its name IS the question
          Group '' -> Text '<the question>' <- repeated, which is how it is found
          Button 'Minimize'                 <- panel chrome, a direct child
          Group ''                          <- the options: buttons, no edit control
            Button '<option>' ...

    The "not under Chat messages" clause is load-bearing. Replayed against a
    session's worth of trees, an earlier label-matching heuristic fired happily
    on messages full of tool-call buttons; this one fires only on a real panel.
    """
    chat = transcript(snap)
    pane = snap.find(type=GROUP, name=PRIMARY_PANE)
    scope = {"under": pane} if pane is not None else {}

    for panel in snap.find_all(type=GROUP, **scope):
        text = panel.label
        if not text or len(text) < 2:
            continue
        if chat is not None and snap.contains(chat, panel):
            continue
        # The name repeated as a text node inside - the recognition signal.
        inside = snap.subtree(panel)
        if not any(n.type == TEXT and n.label == text for n in inside):
            continue

        buttons = [n for n in inside if n.type == BUTTON and n.label]
        chrome = {c.casefold() for c in PANEL_CHROME}
        has_chrome = any(b.label.casefold() in chrome for b in buttons)

        free_text = None
        for name in FREE_TEXT_NAMES:
            free_text = snap.find(type=EDIT, name=name, under=panel)
            if free_text is not None:
                break
        skip = None
        for name in SKIP_NAMES:
            skip = snap.find(type=BUTTON, name=name, under=panel)
            if skip is not None:
                break

        options = [
            b.label for b in buttons
            if b.label.casefold() not in chrome
            and b.label.casefold() not in {s.casefold() for s in SKIP_NAMES}
        ]
        # A panel is only a panel if it offers a way to answer. Without this, any
        # group whose name happens to be repeated inside it would match.
        if not options and free_text is None:
            continue
        if not (has_chrome or free_text is not None or skip is not None):
            continue

        # Deduplicate while keeping order: nested wrappers repeat option labels.
        seen: set[str] = set()
        unique = [o for o in options if not (o in seen or seen.add(o))]
        return Question(node=panel, text=text, options=unique,
                        free_text=free_text, skip=skip)
    return None


# ── the sidebar ──────────────────────────────────────────────────────────────


def strip_row_state(label: str) -> Optional[str]:
    """A sidebar row's chat title, or None if this is not a row.

    The unread toggle is named exactly "Mark as unread" with no title. It has to
    be matched *exactly*, not by prefix, or a real title gets stripped to
    nothing (DRIVERSPEC §4).
    """
    text = (label or "").strip()
    if not text:
        return None
    for decoy in ROW_DECOYS:
        if text.casefold().startswith(decoy.casefold()):
            return None
    for state in ROW_STATES:
        bare = state.strip().casefold()
        if text.casefold() == bare:
            return None  # the toggle itself, not a row
        if text.casefold().startswith(state.casefold()):
            title = text[len(state):].strip()
            return title or None
    return None


def sidebar_rows(snap: Snapshot) -> list[tuple[str, Node]]:
    """(title, button) for every conversation row in the sidebar."""
    side = snap.find(name=SIDEBAR)
    if side is None:
        return []
    rows: list[tuple[str, Node]] = []
    for node in snap.find_all(type=BUTTON, under=side):
        title = strip_row_state(node.label)
        if title:
            rows.append((title, node))
    return rows


def sidebar_titles(snap: Snapshot) -> list[str]:
    return [title for title, _ in sidebar_rows(snap)]


class ThreadNotFound(LookupError):
    pass


class AmbiguousThread(LookupError):
    pass


def find_row(snap: Snapshot, wanted: str) -> Node:
    """The one sidebar row for `wanted`.

    Compared case-insensitively and trimmed, and **never by substring** - "test"
    must not match "Latest ideas". Two chats with the same title is a refusal,
    not a coin flip: picking either means prompts silently go to whichever
    duplicate happened to be showing (DRIVERSPEC §4).
    """
    target = (wanted or "").strip().casefold()
    if not target:
        raise ThreadNotFound("No conversation title was given.")

    rows = sidebar_rows(snap)
    matches = [(title, node) for title, node in rows if title.casefold() == target]
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        raise AmbiguousThread(
            f"{len(matches)} conversations in the sidebar are called {wanted!r}. "
            "Rename one in Claude Desktop so the website can tell them apart - "
            "the bridge will not guess which one you meant."
        )
    known = ", ".join(sorted({t for t, _ in rows})) or "(no rows are rendered)"
    raise ThreadNotFound(
        f"No conversation called {wanted!r} is visible in the Claude Desktop sidebar.\n"
        "The sidebar is virtualised - only the rows currently rendered exist in the "
        "accessibility tree - so this means it is not on screen, which is not the same "
        "as it not existing. Scroll it into view (or open it once, so it sits at the "
        "top of its group) and send again.\n"
        f"Rendered right now: {known}."
    )


# ── the composer ─────────────────────────────────────────────────────────────


class ComposerNotFound(LookupError):
    pass


def composer(snap: Snapshot) -> Node:
    """The prompt box: an edit control that is neither in the transcript nor the
    sidebar (whose search field is also an edit control)."""
    chat = transcript(snap)
    side = snap.find(name=SIDEBAR)

    def outside(node: Node) -> bool:
        if chat is not None and snap.contains(chat, node):
            return False
        if side is not None and snap.contains(side, node):
            return False
        return True

    candidates = [n for n in snap.find_all(type=EDIT) if outside(n)]
    for name in COMPOSER_NAMES:
        for node in candidates:
            if node.label.casefold() == name.casefold():
                return node
    if len(candidates) == 1:
        return candidates[0]
    raise ComposerNotFound(
        f"Could not identify the prompt box: {len(candidates)} candidate edit "
        "controls outside the transcript and sidebar"
        + (": " + ", ".join(repr(c.label) for c in candidates[:6]) if candidates else "")
        + ". Run 'python -m bridge.inspect --grep prompt' and add the right name to "
        "COMPOSER_NAMES in bridge/detect.py."
    )


def streaming_group(snap: Snapshot) -> Optional[Node]:
    """The message being written right now, if there is one.

    Its text is worth reading even though it is incomplete: shown in the website
    as a live preview, it is the difference between a chat that feels alive and
    a spinner that sits there for four minutes.
    """
    return snap.find(type=GROUP, name=STREAMING)


def partial_answer(snap: Snapshot) -> str:
    """Whatever Claude has written so far in the in-flight message, or "".

    Same stitching as a finished message - the streaming group carries the same
    furniture (tool chips, inline spans) and needs the same skips.
    """
    group = streaming_group(snap)
    return read_answer_text(snap, group) if group is not None else ""
