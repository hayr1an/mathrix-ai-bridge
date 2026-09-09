"""Tests for the interpretation layer, against hand-built trees.

No Mac, no Claude Desktop, no accessibility permission: detect.py never touches
Node.ctrl, so the whole surface can be exercised from fake trees. Every case
here is a failure mode DRIVERSPEC names - these are regressions waiting to
happen, not coverage for its own sake.

Trees are (depth, type, name) rows; parent links come from the depth column.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import core as detect
from bridge.core import BUTTON, EDIT, GROUP, TEXT, build

G, T, B, E = GROUP, TEXT, BUTTON, EDIT


def transcript_tree(*messages, extra=()):
    """A window with a sidebar, a transcript and a composer."""
    rows = [
        (0, "WindowControl", "Claude"),
        (1, G, "Sidebar"),
        (2, B, "Idle Price"),
        (3, T, "Price"),
        (2, B, "More options for Price"),
        (2, E, "Search"),
        (1, G, "Primary pane"),
        (2, B, "Price, rename session"),
        (2, G, "Chat messages"),
        (3, G, ""),
        (4, T, "Use the up and down arrow keys to move between messages."),
    ]
    rows.extend(messages)
    rows.extend(extra)
    rows.append((2, E, "Prompt"))
    return build(rows)


def user_message(n, text):
    return [
        (3, G, f"Message {n}"),
        (4, T, f"You said: {text}"),
        (5, T, f"You said: {text}"),
        (4, G, ""),
        (5, T, text),
        (4, G, "Message actions"),
        (5, T, "2 minutes ago"),
        (5, B, "Copy"),
    ]


def claude_message(n, *paragraphs):
    rows = [
        (3, G, f"Message {n}"),
        (4, T, f"Claude responded: {paragraphs[0]}"),
        (5, T, f"Claude responded: {paragraphs[0]}"),
    ]
    for para in paragraphs:
        rows += [(4, G, ""), (5, T, para)]
    rows += [(4, G, "Message actions"), (5, B, "Copy"), (5, T, "just now")]
    return rows


# ── generating ───────────────────────────────────────────────────────────────


def test_streaming_group_means_generating():
    snap = transcript_tree(*user_message(1, "hi"),
                           extra=[(3, G, "Currently streaming message")])
    assert detect.is_generating(snap)


def test_stop_button_outside_transcript_means_generating():
    snap = transcript_tree(*user_message(1, "hi"), extra=[(2, B, "Stop")])
    assert detect.is_generating(snap)


def test_stop_response_is_recognised_too():
    """The Cowork surface names it differently; matching one name silently never
    fires on the other."""
    snap = transcript_tree(*user_message(1, "hi"), extra=[(2, B, "Stop response")])
    assert detect.is_generating(snap)


def test_a_stop_button_inside_a_message_is_not_generation():
    """Buttons inside messages are per-message actions. An unscoped search for
    'Stop' finds them and reports a finished turn as still running."""
    rows = user_message(1, "hi")
    rows.insert(-1, (5, B, "Stop"))
    snap = transcript_tree(*rows)
    assert not detect.is_generating(snap)


def test_idle_transcript_is_not_generating():
    snap = transcript_tree(*user_message(1, "hi"), *claude_message(2, "hello"))
    assert not detect.is_generating(snap)


# ── what is on screen ────────────────────────────────────────────────────────


def test_thread_title_comes_from_the_rename_button():
    snap = transcript_tree(*user_message(1, "hi"))
    assert detect.current_thread_title(snap) == "Price"


def test_message_actions_is_not_counted_as_a_message():
    """'Message actions' shares the 'Message ' prefix and sits inside every
    single message - counting it doubles every count."""
    snap = transcript_tree(*user_message(1, "a"), *claude_message(2, "b"))
    assert detect.message_count(snap) == 2
    assert [g.label for g in detect.message_groups(snap)] == ["Message 1", "Message 2"]


def test_last_answer_is_claudes_not_the_users():
    """Without the announcement filter a failed send returns the user's own
    prompt as the answer, and the job looks like it worked."""
    snap = transcript_tree(*user_message(1, "a"), *claude_message(2, "b"),
                           *user_message(3, "unanswered"))
    group = detect.last_answer_group(snap)
    assert group is not None and group.label == "Message 2"


def test_no_assistant_message_reads_as_none():
    snap = transcript_tree(*user_message(1, "only me"))
    assert detect.last_answer_group(snap) is None


def test_copy_button_is_scoped_to_its_own_message():
    snap = transcript_tree(*user_message(1, "a"), *claude_message(2, "b"))
    group = detect.last_answer_group(snap)
    copy = detect.copy_button(snap, group)
    assert copy is not None and snap.contains(group, copy)


# ── stitching text ───────────────────────────────────────────────────────────


def test_announcements_and_furniture_are_not_part_of_the_answer():
    snap = transcript_tree(*user_message(1, "a"), *claude_message(2, "The answer."))
    text = detect.read_answer_text(snap, detect.last_answer_group(snap))
    assert text == "The answer."
    assert "Claude responded" not in text
    assert "minutes ago" not in text and "just now" not in text


def test_inline_spans_do_not_break_the_line():
    """Inline code/links are text nodes nested in text nodes. Treating each as a
    block split every sentence into three lines around every identifier."""
    snap = transcript_tree(
        (3, G, "Message 1"),
        (4, T, "Claude responded: Run npm run dev to start."),
        (4, G, ""),
        (5, T, "Run"),
        (5, G, ""),
        (6, T, "npm run dev"),
        (5, T, "to start."),
    )
    text = detect.read_answer_text(snap, detect.message_groups(snap)[0])
    # One line, with the inline code inlined - not three lines around it.
    assert text == "Run npm run dev to start."


def test_separate_paragraphs_stay_on_separate_lines():
    """The mirror of the inline case: an unnamed group holding a whole paragraph
    looks structurally identical to one holding a span, and must not be merged."""
    snap = transcript_tree(*claude_message(1, "First para.", "Second para."))
    text = detect.read_answer_text(snap, detect.message_groups(snap)[0])
    assert text.splitlines() == ["First para.", "Second para."]


def test_text_repeated_in_a_child_is_not_pasted_twice():
    snap = transcript_tree(
        (3, G, "Message 1"),
        (4, T, "Claude responded: Hello"),
        (4, G, ""),
        (5, T, "Hello"),
        (6, T, "Hello"),
    )
    text = detect.read_answer_text(snap, detect.message_groups(snap)[0])
    assert text == "Hello"


# ── the sidebar ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label,expected", [
    ("Idle backend_test", "backend_test"),
    ("Running Price", "Price"),
    ("Awaiting answer Price check", "Price check"),
    # A chat with no session state is named after the unread toggle it contains.
    ("Mark as unread backend_test", "backend_test"),
    ("Mark as read Price", "Price"),
    # The toggle itself carries no title and must match exactly, or a real title
    # gets stripped down to nothing.
    ("Mark as unread", None),
    ("Mark as read", None),
    # Nested controls repeat the title without being the row.
    ("More options for Price", None),
    ("Toggle chats for Price", None),
    ("New session in mathrix_ai", None),
    ("Send feedback", None),
])
def test_row_titles(label, expected):
    assert detect.strip_row_state(label) == expected


def test_titles_never_match_by_substring():
    """'test' must not match 'Latest ideas'."""
    snap = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Sidebar"),
        (2, B, "Idle Latest ideas"),
    ])
    with pytest.raises(detect.ThreadNotFound):
        detect.find_row(snap, "test")


def test_titles_match_case_insensitively_and_trimmed():
    snap = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Sidebar"),
        (2, B, "Idle Price"),
    ])
    assert detect.find_row(snap, "  price  ") is not None


def test_duplicate_titles_are_refused_not_guessed():
    """Picking either means prompts silently go to whichever duplicate happened
    to be showing."""
    snap = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Sidebar"),
        (2, B, "Idle Code review"),
        (2, B, "Idle Code review"),
    ])
    with pytest.raises(detect.AmbiguousThread):
        detect.find_row(snap, "Code review")


def test_sidebar_only_sees_rendered_rows():
    """The sidebar is virtualised like the transcript: a chat that exists but is
    scrolled out of view has no node, so the error must not claim it is missing."""
    snap = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Sidebar"),
        (2, B, "Idle Visible one"),
    ])
    with pytest.raises(detect.ThreadNotFound) as exc:
        detect.find_row(snap, "Scrolled away")
    assert "virtualised" in str(exc.value)
    assert "Visible one" in str(exc.value)


def test_unknown_title_lists_what_is_there():
    snap = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Sidebar"),
        (2, B, "Idle Price"),
    ])
    with pytest.raises(detect.ThreadNotFound) as exc:
        detect.find_row(snap, "Nope")
    assert "Price" in str(exc.value)


# ── the composer ─────────────────────────────────────────────────────────────


def test_composer_is_not_the_sidebar_search_box():
    snap = transcript_tree(*user_message(1, "a"))
    box = detect.composer(snap)
    assert box.label == "Prompt"


def test_composer_ignores_edit_controls_inside_the_transcript():
    rows = user_message(1, "a")
    rows.append((4, E, "Edit message"))
    snap = transcript_tree(*rows)
    assert detect.composer(snap).label == "Prompt"


def test_ambiguous_composer_raises_with_the_count():
    snap = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Primary pane"),
        (2, E, "One"),
        (2, E, "Two"),
    ])
    with pytest.raises(detect.ComposerNotFound) as exc:
        detect.composer(snap)
    assert "2 candidate" in str(exc.value)


# ── the question panel ───────────────────────────────────────────────────────


def question_tree(*, inside_transcript=False):
    panel = [
        (2 if not inside_transcript else 3, G, "Which format do you want?"),
        (3, G, ""),
        (4, T, "Which format do you want?"),
        (3, B, "Minimize"),
        (3, G, ""),
        (4, B, "Großfläche"),
        (4, B, "City-Light-Poster"),
        (3, G, ""),
        (4, E, "Something else"),
        (4, B, "Skip"),
    ]
    if inside_transcript:
        return transcript_tree(*user_message(1, "a"), *panel)
    return transcript_tree(*user_message(1, "a"), extra=panel)


def test_question_panel_is_detected_structurally():
    q = detect.pending_question(question_tree())
    assert q is not None
    assert q.text == "Which format do you want?"
    assert q.options == ["Großfläche", "City-Light-Poster"]
    assert q.free_text is not None and q.skip is not None


def test_a_panel_inside_the_transcript_is_not_a_question():
    """The 'not under Chat messages' clause is load-bearing: messages full of
    tool-call buttons matched an earlier label-based heuristic happily."""
    assert detect.pending_question(question_tree(inside_transcript=True)) is None


def test_tool_call_buttons_in_a_message_are_not_a_question():
    snap = transcript_tree(
        (3, G, "Message 1"),
        (4, T, "Claude responded: Working"),
        (4, B, "Ran 2 commands"),
        (5, T, "Ran"),
        (5, T, "2 commands"),
    )
    assert detect.pending_question(snap) is None


def test_a_group_with_no_way_to_answer_is_not_a_question():
    snap = transcript_tree(*user_message(1, "a"), extra=[
        (2, G, "Some heading"),
        (3, T, "Some heading"),
    ])
    assert detect.pending_question(snap) is None


# ── message ordinals (the transcript is virtualised) ─────────────────────────


def test_ordinals_are_read_from_the_label():
    snap = transcript_tree(*user_message(7, "a"), *claude_message(8, "b"))
    groups = detect.message_groups(snap)
    assert [detect.message_number(g) for g in groups] == [7, 8]


def test_ordinals_survive_a_virtualised_transcript():
    """The app only keeps messages near the viewport in the tree - a live read
    showed three groups whose last was 'Message 8'. Counting them says 3, which
    is useless for 'has a new message arrived?'; the ordinal says 8."""
    snap = transcript_tree(*user_message(7, "a"), *claude_message(8, "b"),
                           *user_message(9, "c"))
    assert detect.message_count(snap) == 3
    assert detect.highest_message_number(snap) == 9
    assert detect.last_answer_number(snap) == 8


def test_no_messages_reads_as_zero_not_an_error():
    snap = transcript_tree()
    assert detect.highest_message_number(snap) == 0
    assert detect.last_answer_number(snap) is None


def test_message_actions_has_no_ordinal():
    """It shares the 'Message ' prefix; a looser regex would give it one."""
    snap = transcript_tree(*user_message(1, "a"))
    actions = snap.find(type=G, name="Message actions")
    assert detect.message_number(actions) is None


def test_the_open_conversation_needs_no_rendered_row():
    """The sidebar is virtualised, so the row for the conversation that is
    currently on screen may not exist as a node. Requiring one would fail a job
    that had nothing wrong with it - the rename button is the authority on what
    is open."""
    snap = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Sidebar"),
        (2, B, "Idle Something else"),
        (1, G, "Primary pane"),
        (2, B, "Hello, rename session"),
    ])
    assert detect.current_thread_title(snap) == "Hello"
    assert "Hello" not in detect.sidebar_titles(snap)


def test_the_rename_button_is_the_only_authority_on_what_is_open():
    """A read must be able to tell which conversation it is looking at. Two
    transcripts are structurally identical - same 'Chat messages' group, same
    'Message N' children - so only the rename button distinguishes them. Without
    checking it, a prompt sent to one chat can be answered out of another's
    transcript, which is how it actually failed."""
    other = build([
        (0, "WindowControl", "Claude"),
        (1, G, "Primary pane"),
        (2, B, "Someone else's chat, rename session"),
        (2, G, "Chat messages"),
        (3, G, "Message 9"),
        (4, T, "Claude responded: an answer to a different question"),
    ])
    assert detect.current_thread_title(other) == "Someone else's chat"
    # Its ordinals look perfectly valid on their own - which is the trap.
    assert detect.last_answer_number(other) == 9
