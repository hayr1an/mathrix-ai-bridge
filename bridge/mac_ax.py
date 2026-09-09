"""The thin PyObjC layer: talking to macOS Accessibility, and nothing else.

Everything here is a wrapper around one system call. No interpretation lives in
this file - it is the seam DRIVERSPEC/PORTING describe, and the only part that
would be rewritten for another OS.

Two macOS facts shape it:

* Every AX call can fail, and failure is a returned error code, not an
  exception. A control read a moment ago may already be dead. So each accessor
  returns None/"" rather than raising, and the caller decides.
* AX calls are synchronous IPC into the target app. A busy Electron app can
  stall one indefinitely, which would hang the worker rather than fail it, so
  every element gets an explicit messaging timeout.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import AppKit
import Quartz
from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementIsAttributeSettable,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
    kAXChildrenAttribute,
    kAXDescriptionAttribute,
    kAXErrorSuccess,
    kAXPositionAttribute,
    kAXPressAction,
    kAXRoleAttribute,
    kAXSizeAttribute,
    kAXSubroleAttribute,
    kAXTitleAttribute,
    kAXTrustedCheckOptionPrompt,
    kAXValueAttribute,
    kAXWindowsAttribute,
)

# Asking Chromium to build its accessibility tree and hold it. This is the macOS
# analogue of the Windows "poke the children to wake it" trick (DRIVERSPEC §2);
# it is a private-ish attribute with no constant in the framework bindings.
AX_MANUAL_ACCESSIBILITY = "AXManualAccessibility"
AX_ENHANCED_USER_INTERFACE = "AXEnhancedUserInterface"

# A single AX call that has not answered in this long is treated as lost. The
# app is on the same machine, so anything past a second or two means it is
# wedged, and waiting longer only delays the error.
MESSAGING_TIMEOUT = 2.0


def is_trusted(prompt: bool = False) -> bool:
    """Whether this process may drive other apps.

    Accessibility trust is granted to the *host* binary - the terminal, VS Code,
    or whatever launched Python - not to the script. `prompt=True` opens the
    System Settings pane once; the answer only changes after the host restarts,
    which is worth telling the user rather than retrying.
    """
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: bool(prompt)}))


# ── finding the app ──────────────────────────────────────────────────────────


def app_by_bundle(bundle_id: str) -> Optional[Any]:
    matches = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    return matches[0] if matches else None


def activate(app: Any) -> bool:
    """Bring the app forward. Used only as a fallback - the value-setting path
    deliberately never steals focus (DRIVERSPEC §4)."""
    try:
        # ActivateIgnoringOtherApps is deprecated on recent macOS but still the
        # only thing that reliably raises a background Electron window.
        return bool(app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps))
    except Exception:
        try:
            return bool(app.activate())
        except Exception:
            return False


def is_frontmost(app: Any) -> bool:
    try:
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return bool(front and front.processIdentifier() == app.processIdentifier())
    except Exception:
        return False


def element_for_pid(pid: int) -> Any:
    elem = AXUIElementCreateApplication(pid)
    if elem is not None:
        try:
            AXUIElementSetMessagingTimeout(elem, MESSAGING_TIMEOUT)
        except Exception:
            pass
    return elem


# ── reading elements ─────────────────────────────────────────────────────────


def attr(elem: Any, name: str) -> Any:
    """One attribute, or None.

    Swallows both the error return and any exception from the bridge: a node
    dying mid-walk is normal and must not end the walk (DRIVERSPEC §1).
    """
    if elem is None:
        return None
    try:
        err, value = AXUIElementCopyAttributeValue(elem, name, None)
    except Exception:
        return None
    if err != kAXErrorSuccess:
        return None
    return value


def children(elem: Any) -> list[Any]:
    kids = attr(elem, kAXChildrenAttribute)
    if not kids:
        return []
    try:
        return list(kids)
    except Exception:
        return []


def windows(app_elem: Any) -> list[Any]:
    found = attr(app_elem, kAXWindowsAttribute)
    try:
        return list(found or [])
    except Exception:
        return []


def as_text(value: Any) -> str:
    """An AX value as a plain string.

    AX returns NSString, NSNumber, AXValue (points/sizes) and None through the
    same door. Only text is wanted, so structured values are dropped rather than
    stringified - "<AXValue 0x...>" as an accessible name would poison every
    comparison downstream.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    try:
        if isinstance(value, AppKit.NSString):
            return str(value)
    except Exception:
        pass
    text = str(value)
    # AXValue and other opaque wrappers repr as "<Type 0x...>".
    if text.startswith("<") and "0x" in text:
        return ""
    return text


def role_of(elem: Any) -> tuple[str, str]:
    return as_text(attr(elem, kAXRoleAttribute)), as_text(attr(elem, kAXSubroleAttribute))


def name_of(elem: Any, role: str = "") -> str:
    """The accessible name, by the same precedence VoiceOver announces.

    Chromium puts `aria-label` in AXDescription and the visible string in
    AXTitle, and static text carries its content in AXValue. Title first, then
    description, then value - and for static text, value first, since its title
    is usually empty and its description is the *label*, not the text.
    """
    title = as_text(attr(elem, kAXTitleAttribute))
    desc = as_text(attr(elem, kAXDescriptionAttribute))
    if role == "AXStaticText":
        value = as_text(attr(elem, kAXValueAttribute))
        return (value or title or desc).strip()
    if title:
        return title.strip()
    if desc:
        return desc.strip()
    # A text field's value is user data, not a name; taking it would make the
    # composer's name change as someone types. Only borrow it for controls whose
    # value *is* their label.
    if role in ("AXButton", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXLink"):
        return as_text(attr(elem, kAXValueAttribute)).strip()
    return ""


def value_of(elem: Any) -> Optional[str]:
    raw = attr(elem, kAXValueAttribute)
    return None if raw is None else as_text(raw)


def actions(elem: Any) -> list[str]:
    try:
        err, names = AXUIElementCopyActionNames(elem, None)
    except Exception:
        return []
    return [str(n) for n in (names or [])] if err == kAXErrorSuccess else []


def settable(elem: Any, name: str) -> bool:
    try:
        err, flag = AXUIElementIsAttributeSettable(elem, name, None)
    except Exception:
        return False
    return err == kAXErrorSuccess and bool(flag)


# ── acting on elements ───────────────────────────────────────────────────────


def set_attr(elem: Any, name: str, value: Any) -> bool:
    """True only if the API reported success. The caller still has to verify the
    effect - a SetValue that silently no-ops looks identical (DRIVERSPEC §4)."""
    try:
        return AXUIElementSetAttributeValue(elem, name, value) == kAXErrorSuccess
    except Exception:
        return False


def perform(elem: Any, action: str = kAXPressAction) -> bool:
    try:
        return AXUIElementPerformAction(elem, action) == kAXErrorSuccess
    except Exception:
        # An exception here often means the press *worked* and destroyed the
        # element it was called on (DRIVERSPEC §4). The activation loop checks
        # the goal either way, so this is reported as failure and forgiven.
        return False


def frame_of(elem: Any) -> Optional[tuple[float, float, float, float]]:
    """(x, y, width, height) in screen points, or None."""
    pos, size = attr(elem, kAXPositionAttribute), attr(elem, kAXSizeAttribute)
    if pos is None or size is None:
        return None
    try:
        from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType

        ok_p, point = AXValueGetValue(pos, kAXValueCGPointType, None)
        ok_s, dims = AXValueGetValue(size, kAXValueCGSizeType, None)
        if not (ok_p and ok_s):
            return None
        return float(point.x), float(point.y), float(dims.width), float(dims.height)
    except Exception:
        return None


def centre_of(elem: Any) -> Optional[tuple[float, float]]:
    frame = frame_of(elem)
    if frame is None:
        return None
    x, y, w, h = frame
    if w <= 0 or h <= 0:
        return None
    return x + w / 2.0, y + h / 2.0


def click(x: float, y: float) -> None:
    """A real mouse click at a screen point - the last resort in the activation
    ladder, for controls that expose no working AXPress."""
    where = Quartz.CGPointMake(x, y)
    for kind in (Quartz.kCGEventMouseMoved,
                 Quartz.kCGEventLeftMouseDown,
                 Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, kind, where, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)


# Virtual key codes for the typing fallback.
KEY_A, KEY_V, KEY_RETURN = 0, 9, 36


def key(code: int, command: bool = False) -> None:
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if command:
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)


# ── clipboard ────────────────────────────────────────────────────────────────


def pasteboard_set(text: str) -> None:
    board = AppKit.NSPasteboard.generalPasteboard()
    board.clearContents()
    if text:
        board.setString_forType_(text, AppKit.NSPasteboardTypeString)


def pasteboard_get() -> str:
    board = AppKit.NSPasteboard.generalPasteboard()
    return str(board.stringForType_(AppKit.NSPasteboardTypeString) or "")


def pasteboard_clear() -> int:
    """Empty the clipboard and return its new change count.

    Clearing before a Copy is not optional: a stale clipboard is
    indistinguishable from a successful copy (DRIVERSPEC §3). The change count
    is the reliable "did anything land" signal - copied text can legitimately
    equal what was there before.
    """
    board = AppKit.NSPasteboard.generalPasteboard()
    return int(board.clearContents())


def pasteboard_change_count() -> int:
    return int(AppKit.NSPasteboard.generalPasteboard().changeCount())
