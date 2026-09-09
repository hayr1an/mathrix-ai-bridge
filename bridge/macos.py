"""Driving Claude Desktop on macOS: walking its tree, waking it, acting on it.

This is the half that has to cope with an app that redraws under it, controls
that report success without doing anything, and clicks that raise *because* they
worked. The rule the whole file is built on (DRIVERSPEC §4): never trust a
return value, always re-resolve in a fresh snapshot, and decide by asking
whether the thing you wanted actually happened.

`mac_ax` stays separate from this: it is one wrapper per system call and nothing
else, and it is the only file that would be rewritten for another OS.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import core, mac_ax
from .core import (
    BUTTON, EDIT, GROUP, IMAGE, LIST, LIST_ITEM, PANE, TEXT, UNKNOWN, WINDOW,
    Node, Snapshot,
)


# ── walking the live tree ─────────────────────────────────────────────────────

# macOS AX roles -> the Windows UIA vocabulary the interpretation layer speaks.
# Normalising here rather than in core.py is deliberate (DRIVERSPEC §5.4): the
# shared logic and its tests never learn there are two platforms.
ROLE_MAP = {
    "AXStaticText": TEXT,
    "AXHeading": TEXT,
    "AXButton": BUTTON,
    "AXMenuButton": BUTTON,
    "AXPopUpButton": BUTTON,
    "AXCheckBox": BUTTON,
    "AXRadioButton": BUTTON,
    "AXLink": BUTTON,
    "AXDisclosureTriangle": BUTTON,
    "AXTextField": EDIT,
    "AXTextArea": EDIT,
    "AXComboBox": EDIT,
    "AXSearchField": EDIT,
    "AXGroup": GROUP,
    "AXSplitGroup": GROUP,
    "AXScrollArea": GROUP,
    "AXWebArea": GROUP,
    "AXTabGroup": GROUP,
    "AXToolbar": GROUP,
    "AXOutline": LIST,
    "AXList": LIST,
    "AXTable": LIST,
    "AXRow": LIST_ITEM,
    "AXCell": LIST_ITEM,
    "AXMenuItem": LIST_ITEM,
    "AXWindow": WINDOW,
    "AXImage": IMAGE,
    "AXSplitter": PANE,
    "AXScrollBar": PANE,
    "AXUnknown": UNKNOWN,
}

# Never walked into. The menu bar is thousands of nodes of application menus
# that no lookup here cares about, and AXMenuItem children can lead back to the
# application element - a cycle that ate the whole node budget the first time
# this ran. AXApplication is in the list for the same reason: a dormant app
# answers AXWindows with *itself*, repeatedly.
SKIP_ROLES = {
    "AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem", "AXApplication",
}


def normalise_role(role: str, subrole: str = "") -> str:
    if not role:
        return UNKNOWN
    # A subrole is more specific when it exists and is mapped - an AXGroup with
    # subrole AXStandardWindow is a window, not a group.
    if subrole and subrole in ROLE_MAP:
        return ROLE_MAP[subrole]
    return ROLE_MAP.get(role, UNKNOWN)


class TreeAsleep(RuntimeError):
    """The app answered, but with frame chrome only.

    Named for *accessibility* on purpose. The old failure mode was letting the
    stub through, after which every lookup failed and the error read as a wrong
    thread title - "conversation matching 'Who are you' not found" was this
    (DRIVERSPEC §2).
    """


class AppNotRunning(RuntimeError):
    pass


class NotTrusted(RuntimeError):
    pass


class Walker:
    """Owns the connection to one running app: its element, its wake state, and
    the walk itself."""

    def __init__(self, cfg: dict[str, Any], *, ax: Any = None):
        self.cfg = cfg
        self.ax = ax or mac_ax
        self.bundle_id = str(cfg.get("bundle_id") or "com.anthropic.claudefordesktop")
        self.max_depth = int(cfg.get("walk_depth", 80))
        self.node_budget = int(cfg.get("walk_node_budget", 20000))
        self.stub_nodes = int(cfg.get("stub_tree_nodes", 50))
        self._app: Any = None
        self._elem: Any = None
        self._pid: Optional[int] = None
        # Never latched permanently: a tree that goes back to sleep must be
        # re-wakeable (DRIVERSPEC §2, rule 1).
        self._awake = False

    # ── the app ──────────────────────────────────────────────────────────────

    def require_trust(self) -> None:
        if not self.ax.is_trusted(prompt=False):
            raise NotTrusted(
                "This process is not trusted for Accessibility, so it cannot read or "
                "drive Claude Desktop.\n"
                "Grant it in System Settings -> Privacy & Security -> Accessibility, "
                "to the app that launched the bridge (Terminal, iTerm or VS Code), "
                "then restart that app - the permission is only picked up at launch."
            )

    def app(self) -> Any:
        """The running Claude Desktop, re-resolved if it restarted.

        Comparing pids matters: after a relaunch the old AXUIElement keeps
        answering with errors forever, which looks exactly like a dormant tree.
        """
        running = self.ax.app_by_bundle(self.bundle_id)
        if running is None:
            raise AppNotRunning(
                f"Claude Desktop ({self.bundle_id}) is not running. Start it, open the "
                "conversation you want the website to use, and try again."
            )
        pid = int(running.processIdentifier())
        if self._elem is None or pid != self._pid:
            self._app = running
            self._pid = pid
            self._elem = self.ax.element_for_pid(pid)
            self._awake = False
        return self._app

    def element(self) -> Any:
        self.app()
        return self._elem

    # ── waking ───────────────────────────────────────────────────────────────

    def _poke(self) -> None:
        """Ask Chromium to build its tree, then touch it.

        AXManualAccessibility is the lever for Chromium-based apps. Two things
        about it, both found the hard way on this machine:

        * Reading it back gives False even when the set succeeded. It is a
          trigger, not a stored flag, so the return code is the only signal and
          even that is not worth trusting.
        * The tree is not there the instant it returns. Before this call the app
          answered AXWindows with two copies of *itself*; about a second after
          it, with three real AXWindows. Hence the settle in wake().
        """
        elem = self._elem
        self.ax.set_attr(elem, mac_ax.AX_MANUAL_ACCESSIBILITY, True)
        # Not settable on this build (-25208) and harmless when it fails; kept
        # because other Electron versions do respond to it.
        self.ax.set_attr(elem, mac_ax.AX_ENHANCED_USER_INTERFACE, True)
        for window in self._real_windows():
            self.ax.children(window)

    def _real_windows(self) -> list[Any]:
        """The app's actual windows.

        A dormant Chromium app answers AXWindows with the application element
        itself - which then has the application as *its* child, and so on. Walking
        that produced 20k nodes of menu bar and no UI. Anything that is not an
        AXWindow is dropped here rather than being diagnosed downstream.
        """
        out = []
        for window in self.ax.windows(self._elem):
            try:
                role, _ = self.ax.role_of(window)
            except Exception:
                continue
            if role == "AXWindow":
                out.append(window)
        return out

    def wake(self, *, force: bool = False) -> Snapshot:
        """A Snapshot of a tree that is actually populated.

        A dormant Chromium window still answers - with a few dozen nodes of
        frame chrome, which reads exactly like an app with no UI. So the test is
        the node count, not whether the call succeeded.
        """
        self.require_trust()
        self.element()

        if self._awake and not force:
            snap = self.snapshot()
            if len(snap) >= self.stub_nodes:
                return snap
            self._awake = False  # it went back to sleep; fall through and re-wake

        settle = float(self.cfg.get("settle_seconds", 2.5))
        deadline = time.time() + float(self.cfg.get("wake_timeout", 30.0))
        best = 0
        attempts = 0
        while True:
            self._poke()
            time.sleep(settle)
            snap = self.snapshot()
            best = max(best, len(snap))
            if len(snap) >= self.stub_nodes:
                self._awake = True
                return snap

            attempts += 1
            # Rebuild the application element before trying again. A handle can
            # go bad while the pid stays the same - the app rebuilds its window
            # after a session switch, say - and a dead handle answers every
            # query with the application itself, forever. Observed live: 0 nodes
            # for a full 30s of retries that were all poking the same corpse.
            self._elem = self.ax.element_for_pid(self._pid)

            # Still nothing after a few rounds: the window may be minimised or
            # on another Space, where Chromium will not build a tree at all.
            # Raising it is intrusive, which is why it is not the first move.
            if attempts == 3:
                try:
                    self.ax.activate(self._app)
                except Exception:
                    pass

            if time.time() >= deadline:
                self._awake = False
                raise TreeAsleep(
                    "Claude Desktop's accessibility tree never woke up: the best read "
                    f"was {best} nodes, which is window chrome only (under "
                    f"{self.stub_nodes}).\n"
                    "This is an accessibility problem, not a missing conversation. "
                    "Check that the app is running with a window open and not "
                    "minimised, that this process has Accessibility permission, and "
                    "try raising settle_seconds in bridge/config.json."
                )

    # ── walking ──────────────────────────────────────────────────────────────

    def _props(self, ctrl: Any) -> tuple[str, str, str]:
        """(normalised type, name, raw role) for one element, never raising."""
        try:
            role, subrole = self.ax.role_of(ctrl)
        except Exception:
            return UNKNOWN, "", ""
        try:
            name = self.ax.name_of(ctrl, role)
        except Exception:
            name = ""
        return normalise_role(role, subrole), name, role

    def snapshot(self) -> Snapshot:
        """Flatten the app's tree, depth-first, pre-order.

        Every property read is wrapped and a failing node is skipped rather than
        aborting: the app redraws constantly and a control read at the start of
        the walk can be dead before its next property is read. Losing one node
        is survivable; losing the snapshot fails the job (DRIVERSPEC §1).
        """
        self.element()
        nodes: list[Node] = []
        # Breaks cycles. AX elements compare and hash by identity through
        # PyObjC, so a node reached twice by two paths is visited once.
        seen: set[Any] = set()

        # Explicit stack, not recursion: 80 levels of Electron nesting times the
        # frames PyObjC adds would sit uncomfortably close to Python's limit.
        stack: list[tuple[Any, int, Optional[Node]]] = []
        for window in reversed(self._real_windows()):
            stack.append((window, 0, None))

        while stack and len(nodes) < self.node_budget:
            ctrl, depth, parent = stack.pop()
            try:
                if ctrl in seen:
                    continue
                seen.add(ctrl)
            except Exception:
                pass  # unhashable handle: walk it, the depth limit still bounds us

            ctype, name, raw = self._props(ctrl)
            if raw in SKIP_ROLES and depth > 0:
                continue

            node = Node(index=len(nodes), depth=depth, type=ctype, name=name,
                        ctrl=ctrl, parent=parent, extra={"role": raw})
            nodes.append(node)

            if depth >= self.max_depth:
                continue
            try:
                kids = self.ax.children(ctrl)
            except Exception:
                continue
            # Reversed, because the stack pops last-in first: children must be
            # visited left to right for the flat list to be in reading order,
            # which is what "oldest message first" and subtree arithmetic assume.
            for kid in reversed(kids):
                stack.append((kid, depth + 1, node))

        return Snapshot(nodes, taken_at=time.time())

    def fresh(self) -> Snapshot:
        """A snapshot guaranteed to be populated - what every action re-resolves
        against, since references go stale (DRIVERSPEC §4)."""
        return self.wake()


# ── driving it ────────────────────────────────────────────────────────────────

Locate = Callable[[Snapshot], Optional[Node]]
Done = Callable[[Snapshot], bool]


class SendFailed(RuntimeError):
    pass


class AnswerNotFound(RuntimeError):
    pass


class Timeout(RuntimeError):
    pass


@dataclass(frozen=True)
class SendMark:
    """Where the transcript stood immediately before a prompt was sent.

    Both fields are message *ordinals*, not counts. Counting is not usable here:
    the app virtualises the transcript, so the number of message nodes reflects
    what is scrolled into view, and one message can scroll out as another
    scrolls in leaving the count identical. Ordinals are absolute.

    Two fields rather than one because they answer different questions.
    `highest` proves the send landed - it moves as soon as the user's own
    message appears. `last_answer` proves the *reply* landed, which happens
    later: while Claude is writing, its message is called "Currently streaming
    message" and has no ordinal at all, so the last numbered answer is still the
    previous turn's.
    """

    #: Highest message ordinal anywhere in the transcript.
    highest: int
    #: Ordinal of the last message from Claude; None if there was none.
    last_answer: Optional[int]


class Driver:
    """One live connection to Claude Desktop.

    Cheap to construct and safe to keep for the life of the worker: it holds no
    element references between calls, only the app handle.
    """

    def __init__(self, cfg: dict[str, Any], *, walker: Optional[Walker] = None):
        self.cfg = cfg
        self.walker = walker or Walker(cfg)
        self.poll = float(cfg.get("poll_interval", 1.0))

    # ── snapshots ────────────────────────────────────────────────────────────

    def fresh(self) -> Snapshot:
        """A populated snapshot. Anything that acts re-resolves against one of
        these, because references go stale within seconds."""
        return self.walker.wake()

    # ── read-only (all of this is core.py, scoped to a live tree) ──────────

    def is_generating(self, snap: Optional[Snapshot] = None) -> bool:
        return core.is_generating(snap or self.fresh())

    def current_thread_title(self, snap: Optional[Snapshot] = None) -> Optional[str]:
        return core.current_thread_title(snap or self.fresh())

    def message_count(self, snap: Optional[Snapshot] = None) -> int:
        return core.message_count(snap or self.fresh())

    def pending_question(self, snap: Optional[Snapshot] = None) -> Optional[core.Question]:
        return core.pending_question(snap or self.fresh())

    def sidebar_titles(self, snap: Optional[Snapshot] = None) -> list[str]:
        return core.sidebar_titles(snap or self.fresh())

    # ── the activation ladder ────────────────────────────────────────────────

    def _routes(self) -> list[tuple[str, Callable[[Node], None]]]:
        """Ways to activate a control, cheapest and least intrusive first.

        The physical click is last because it needs the app frontmost, which is
        the one thing the rest of this driver goes out of its way to avoid.
        """
        def press(node: Node) -> None:
            mac_ax.perform(node.ctrl, "AXPress")

        def alternate(node: Node) -> None:
            for action in mac_ax.actions(node.ctrl):
                if action in ("AXPress", "AXShowMenu", "AXScrollToVisible"):
                    continue
                mac_ax.perform(node.ctrl, action)

        def focus_then_press(node: Node) -> None:
            mac_ax.perform(node.ctrl, "AXScrollToVisible")
            mac_ax.set_attr(node.ctrl, "AXFocused", True)
            time.sleep(0.1)
            mac_ax.perform(node.ctrl, "AXPress")

        def click(node: Node) -> None:
            centre = mac_ax.centre_of(node.ctrl)
            if centre is None:
                return
            app = self.walker.app()
            if not mac_ax.is_frontmost(app):
                mac_ax.activate(app)
                time.sleep(0.4)
            mac_ax.click(*centre)

        return [("press", press), ("alternate", alternate),
                ("focus+press", focus_then_press), ("click", click)]

    def activate(self, locate: Locate, done: Done, *, settle: float = 0.6) -> bool:
        """Make something happen, and verify it happened.

        Three facts force this shape (DRIVERSPEC §4):

        * A silent no-op reads as success - AXPress returns kAXErrorSuccess for
          a control that does nothing.
        * An exception often means it worked: a press that succeeds can destroy
          the element it was called on, and the failure surfaces here.
        * References go stale, so the control is re-resolved by label in a fresh
          snapshot before every attempt.

        Hence: try a route, then check the *goal*, and check it again even if the
        route raised.
        """
        for _name, route in self._routes():
            snap = self.fresh()
            if done(snap):
                return True
            node = locate(snap)
            if node is None:
                continue
            try:
                route(node)
            except Exception:  # noqa: BLE001 - the goal check is the verdict
                pass
            time.sleep(settle)
            if done(self.fresh()):
                return True
        return False

    # ── focus ────────────────────────────────────────────────────────────────

    def focus(self, retries: Optional[int] = None) -> bool:
        """Bring Claude Desktop forward, reporting whether it worked.

        A boolean on purpose: the original returned None whether or not it
        succeeded, and the caller typed regardless - into whatever window
        actually had focus (DRIVERSPEC §4).
        """
        app = self.walker.app()
        for _ in range(int(retries if retries is not None else self.cfg.get("focus_retries", 3))):
            if mac_ax.is_frontmost(app):
                return True
            mac_ax.activate(app)
            time.sleep(0.5)
        return mac_ax.is_frontmost(app)

    # ── opening a conversation ───────────────────────────────────────────────

    def open_thread(self, title: str, *, snap: Optional[Snapshot] = None) -> Snapshot:
        """Make `title` the conversation on screen.

        No fast path for "it is already open". The original returned early on
        that check, which meant that with two chats of the same name every
        prompt silently went to whichever duplicate happened to be showing;
        find_row refuses duplicates, so the check has to run either way
        (DRIVERSPEC §4).
        """
        snap = snap or self.fresh()
        wanted = title.strip().casefold()
        current = core.current_thread_title(snap)

        if current is not None and current.strip().casefold() == wanted:
            # Already showing it. The duplicate check still runs - that is the
            # one thing a fast path must never skip - but a rendered sidebar row
            # is deliberately *not* required here. The sidebar is virtualised,
            # so the row for the open conversation may not exist as a node at
            # all, and demanding one fails a job that had nothing wrong with it.
            # What is on screen is stronger evidence than what happens to be
            # scrolled into view.
            same = [t for t, _ in core.sidebar_rows(snap)
                    if t.strip().casefold() == wanted]
            if len(same) > 1:
                raise core.AmbiguousThread(
                    f"{len(same)} conversations in the sidebar are called {title!r}. "
                    "Rename one in Claude Desktop so the website can tell them apart - "
                    "the bridge will not guess which one you meant."
                )
            return snap

        core.find_row(snap, title)  # raises on an unknown or duplicated title

        def locate(s: Snapshot) -> Optional[Node]:
            try:
                return core.find_row(s, title)
            except LookupError:
                return None

        def opened(s: Snapshot) -> bool:
            shown = core.current_thread_title(s)
            return shown is not None and shown.strip().casefold() == wanted

        if not self.activate(locate, opened):
            shown = core.current_thread_title(self.fresh())
            raise SendFailed(
                f"Could not switch Claude Desktop to {title!r}; it is still showing "
                f"{shown!r}. The row was found in the sidebar but clicking it had no "
                "effect - try opening that conversation by hand once."
            )
        return self.fresh()

    def require_thread(self, title: str, snap: Optional[Snapshot] = None) -> Snapshot:
        """Re-check, immediately before typing.

        The app drifts on its own: a Code session becoming active pulls the
        window to that surface. Everything downstream reads whatever is
        displayed, so without this the prompt lands in the wrong conversation
        and someone else's reply comes back as the answer (DRIVERSPEC §4).
        """
        snap = snap or self.fresh()
        shown = core.current_thread_title(snap)
        if shown is not None and shown.strip().casefold() == title.strip().casefold():
            return snap
        return self.open_thread(title, snap=snap)

    # ── sending ──────────────────────────────────────────────────────────────

    def _placeholder(self, node: Node) -> str:
        return mac_ax.as_text(mac_ax.attr(node.ctrl, "AXPlaceholderValue")).strip()

    def _composer_text(self, node: Node) -> str:
        """What the composer holds, with the placeholder read as empty.

        The prompt box is a contenteditable, and an empty one reports its
        placeholder ("Type / for commands") as its AXValue. Taken literally that
        looks like text the user typed.
        """
        value = (mac_ax.value_of(node.ctrl) or "").strip()
        placeholder = self._placeholder(node)
        if placeholder and value == placeholder:
            return ""
        return value

    def _set_composer(self, text: str) -> bool:
        """Put `text` in the prompt box without stealing focus.

        The primary path, deliberately: typing goes to whatever window has focus,
        and with a browser in front - which is exactly the case here, the user
        just clicked Send on the website - synthesised keys perform a
        select-all-and-replace *in the browser* (DRIVERSPEC §4).

        Reads back, because a SetValue that silently no-ops looks identical to
        one that worked. The read-back is necessary but not sufficient: this is a
        contenteditable, so AX can update the DOM without the app's framework
        noticing. Only the transcript growing proves the send - see send_prompt.
        """
        node = core.composer(self.fresh())
        if not mac_ax.set_attr(node.ctrl, "AXValue", text):
            return False
        time.sleep(0.3)
        back = self._composer_text(core.composer(self.fresh()))
        return back.strip() == text.strip()

    def _paste_composer(self, text: str) -> bool:
        """Fallback: focus the app and paste.

        Goes through the app's own input path, so it takes in cases where
        setting the value does not. It steals focus, which is why it is second.
        """
        if not self.focus():
            return False
        node = core.composer(self.fresh())
        mac_ax.perform(node.ctrl, "AXScrollToVisible")
        mac_ax.set_attr(node.ctrl, "AXFocused", True)
        time.sleep(0.2)
        mac_ax.pasteboard_set(text)
        mac_ax.key(mac_ax.KEY_A, command=True)   # replace anything already there
        time.sleep(0.1)
        mac_ax.key(mac_ax.KEY_V, command=True)
        time.sleep(0.4)
        return self._composer_text(core.composer(self.fresh())).strip() == text.strip()

    def _submit(self) -> None:
        """Send what is in the composer.

        The Send button when the app is showing one; Return otherwise - while a
        turn is running the button is replaced by Stop, and after a fresh launch
        it can be absent until the box has content.
        """
        button = core.send_button(self.fresh())
        if button is not None and mac_ax.perform(button.ctrl, "AXPress"):
            return
        node = core.composer(self.fresh())
        mac_ax.set_attr(node.ctrl, "AXFocused", True)
        time.sleep(0.15)
        mac_ax.key(mac_ax.KEY_RETURN)

    def mark(self, snap: Optional[Snapshot] = None) -> SendMark:
        snap = snap or self.fresh()
        return SendMark(highest=core.highest_message_number(snap),
                        last_answer=core.last_answer_number(snap))

    def _still_on(self, thread_title: str) -> None:
        """The app drifts on its own; if it moved, the text is now in the wrong
        conversation and sending would be worse than failing."""
        shown = core.current_thread_title(self.fresh())
        if shown is not None and shown.strip().casefold() != thread_title.strip().casefold():
            raise SendFailed(
                f"Claude Desktop moved to {shown!r} while the prompt was being typed; "
                f"nothing was sent to {thread_title!r}."
            )

    def send_prompt(self, text: str, thread_title: str) -> SendMark:
        """Type `text` into `thread_title`, send it, and prove it landed.

        Two ways in, tried in order, each confirmed by the transcript actually
        growing rather than by any return value.

        Retrying is safe, and only because of what wait_for_start means: it
        raises only after the transcript has stayed the same size *and* the app
        has reported itself idle for the whole confirmation window. In that
        state nothing was submitted, so the second attempt cannot duplicate a
        message that is already on its way.
        """
        text = (text or "").strip()
        if not text:
            raise SendFailed("Refusing to send an empty prompt.")

        snap = self.require_thread(thread_title)
        mark = self.mark(snap)

        problems: list[str] = []
        for place, described in ((self._set_composer, "setting the box's value"),
                                 (self._paste_composer, "pasting into the box")):
            if not place(text):
                problems.append(f"{described} did not take")
                continue
            self._still_on(thread_title)
            self._submit()
            try:
                self.wait_for_start(mark, thread_title)
                return mark
            except SendFailed:
                problems.append(f"{described} worked but nothing was sent")

        raise SendFailed(
            "Could not get the prompt into Claude Desktop: "
            + "; ".join(problems)
            + ". Check that the conversation is open and the message box is not "
            "disabled by a dialog. Nothing was read back - no previous answer has "
            "been mistaken for this one."
        )

    def wait_for_start(self, mark: SendMark, thread_title: str,
                       timeout: Optional[float] = None) -> None:
        """Prove the send landed: the transcript has to grow, or generation has
        to start. Without this a failed send reads the previous answer back as
        this job's result (DRIVERSPEC §3)."""
        limit = float(timeout if timeout is not None else self.cfg.get("send_confirm_timeout", 25))
        deadline = time.time() + limit
        while time.time() < deadline:
            snap = self.fresh()
            if core.highest_message_number(snap) > mark.highest or core.is_generating(snap):
                return
            time.sleep(self.poll)
        raise SendFailed(
            f"The prompt was typed into {thread_title!r} but no message past "
            f"#{mark.highest} appeared in {limit:.0f}s, so it was never sent."
        )

    # ── waiting for the turn ─────────────────────────────────────────────────

    def wait_for_idle(self, *, timeout: Optional[float] = None,
                      grace: Optional[float] = None,
                      stop_when: Optional[Callable[[Snapshot], bool]] = None,
                      on_tick: Optional[Callable[[Snapshot], None]] = None) -> Snapshot:
        """Block until the turn is over.

        Generation must read false *continuously* for `grace` seconds: a single
        false reading between tokens is not the end of a turn.

        `stop_when` is the early exit. Claude can pause to ask a question, and
        the app still reports itself as generating while that panel sits there -
        without this the job burns the whole timeout waiting for a panel that
        only needs a click (DRIVERSPEC §3).
        """
        limit = float(timeout if timeout is not None else self.cfg.get("generation_timeout", 900))
        settle = float(grace if grace is not None else self.cfg.get("idle_grace", 3.0))
        deadline = time.time() + limit
        idle_since: Optional[float] = None

        while time.time() < deadline:
            snap = self.fresh()
            if on_tick is not None:
                on_tick(snap)
            if stop_when is not None and stop_when(snap):
                return snap
            if core.is_generating(snap):
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since >= settle:
                    return snap
            time.sleep(self.poll)

        raise Timeout(
            f"Claude Desktop was still generating after {limit:.0f}s. The answer may "
            "still arrive in the app; the website gave up waiting for it."
        )

    # ── reading the answer ───────────────────────────────────────────────────

    def read_answer(self, *, after: Optional[SendMark] = None,
                    timeout: Optional[float] = None) -> str:
        """The answer to *this* prompt, preferring Claude's own Copy button.

        Waits for a genuinely new assistant message before reading anything.
        While Claude is still writing, its message is called "Currently
        streaming message" and is not a numbered message at all - so the last
        *answer* group is still the previous turn's, and the message count has
        already grown by one from the user's own message. Reading in that window
        would hand back the previous answer while looking entirely valid.

        Copy puts exact markdown on the clipboard - no menu to open, nothing to
        dismiss. The clipboard is cleared first because a stale clipboard is
        indistinguishable from a successful copy, and the change count is
        watched rather than the text, since a copy can legitimately reproduce
        what was already there.
        """
        limit = float(timeout if timeout is not None else 20.0)
        deadline = time.time() + limit
        snap = self.fresh()
        group = core.last_answer_group(snap)

        while after is not None:
            number = core.message_number(group) if group is not None else None
            # Strictly greater, not merely different: scrolling can bring an
            # *older* answer into view, and "different" would happily read that
            # one back.
            if number is not None and (after.last_answer is None or number > after.last_answer):
                break
            if time.time() >= deadline:
                raise AnswerNotFound(
                    "Claude finished its turn but no new message from it appeared in "
                    f"the transcript within {limit:.0f}s (newest is "
                    f"#{number if number is not None else '-'}, expected past "
                    f"#{after.last_answer}). Refusing to read the previous answer back "
                    "as this one."
                )
            time.sleep(self.poll)
            snap = self.fresh()
            group = core.last_answer_group(snap)

        if group is None:
            raise AnswerNotFound(
                "No message from Claude in the transcript. Every message is announced "
                "as 'You said:' or 'Claude responded:' and none of the assistant kind "
                "were found - the send may have gone nowhere."
            )

        copied = self._copy_answer(group)
        if copied.strip():
            return copied.strip()

        stitched = core.read_answer_text(snap, group)
        if stitched.strip():
            return stitched.strip()
        raise AnswerNotFound(
            "Claude's last message read as empty, both from its Copy button and by "
            "stitching the transcript text."
        )

    def _copy_answer(self, group: Node) -> str:
        """Invoke a message's Copy button and read the clipboard, or "" ."""
        label = group.label
        before_count = mac_ax.pasteboard_clear()

        def locate(s: Snapshot) -> Optional[Node]:
            # Re-resolved by *label* in a fresh snapshot, never by holding the
            # node or its index: the tree is rebuilt constantly, so the old
            # handle is dead and a flat-list index can land on a different
            # message entirely. "Message 6" stays "Message 6".
            target = next((g for g in core.message_groups(s) if g.label == label), None)
            if target is None:
                target = core.last_answer_group(s)
            return core.copy_button(s, target) if target is not None else None

        def copied(_s: Snapshot) -> bool:
            return mac_ax.pasteboard_change_count() > before_count

        if not self.activate(locate, copied, settle=0.8):
            return ""
        time.sleep(0.3)
        return mac_ax.pasteboard_get()

    # ── the question panel ───────────────────────────────────────────────────

    def answer_question(self, *, choice: Optional[str] = None,
                        text: Optional[str] = None, skip: bool = False) -> bool:
        """Answer the panel on screen. Exactly one of choice / text / skip.

        Done, in every case, is "the panel is gone" - the same goal-checking as
        every other action, because clicking an option destroys the panel and
        the click therefore often reports failure.
        """
        given = [choice is not None, text is not None, bool(skip)]
        if sum(given) != 1:
            raise ValueError("answer with exactly one of: choice, text, skip")

        question = self.pending_question()
        if question is None:
            raise AnswerNotFound("There is no question on screen to answer.")
        asked = question.text

        def gone(s: Snapshot) -> bool:
            current = core.pending_question(s)
            return current is None or current.text != asked

        if text is not None:
            return self._answer_with_text(asked, text, gone)

        if skip:
            wanted = None
        else:
            if choice not in question.options:
                raise ValueError(
                    f"{choice!r} is not one of the options on screen "
                    f"({', '.join(question.options) or 'none'})."
                )
            wanted = choice

        def locate(s: Snapshot) -> Optional[Node]:
            current = core.pending_question(s)
            if current is None:
                return None
            if wanted is None:
                return current.skip
            return s.find(type="ButtonControl", name=wanted, under=current.node)

        return self.activate(locate, gone)

    def _answer_with_text(self, asked: str, text: str, gone: Done) -> bool:
        question = self.pending_question()
        if question is None or question.free_text is None:
            raise AnswerNotFound(
                "This question has no free-text box; pick one of the options instead."
            )
        box = question.free_text
        if not mac_ax.set_attr(box.ctrl, "AXValue", text):
            if not self.focus():
                return False
            mac_ax.set_attr(box.ctrl, "AXFocused", True)
            mac_ax.pasteboard_set(text)
            time.sleep(0.1)
            mac_ax.key(mac_ax.KEY_V, command=True)
        time.sleep(0.3)
        mac_ax.set_attr(box.ctrl, "AXFocused", True)
        mac_ax.key(mac_ax.KEY_RETURN)
        time.sleep(0.6)
        return gone(self.fresh())

    # ── health ───────────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Everything the website needs to explain itself when something is off."""
        out: dict[str, Any] = {"ok": False, "trusted": mac_ax.is_trusted(False),
                               "app_running": False, "tree_awake": False,
                               "thread": None, "generating": False,
                               "chats": [], "error": None}
        try:
            snap = self.fresh()
        except (NotTrusted, AppNotRunning, TreeAsleep) as exc:
            out["error"] = str(exc)
            out["app_running"] = not isinstance(exc, AppNotRunning)
            return out
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
            return out

        out.update(ok=True, app_running=True, tree_awake=True,
                   thread=core.current_thread_title(snap),
                   generating=core.is_generating(snap),
                   chats=core.sidebar_titles(snap),
                   nodes=len(snap))
        return out
