"""Tests for the flat-tree arithmetic everything else is built on."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.core import BUTTON, GROUP, TEXT, build

G, T, B = GROUP, TEXT, BUTTON


def sample():
    return build([
        (0, G, "root"),
        (1, G, "a"),
        (2, T, "a1"),
        (2, T, "a2"),
        (1, G, "b"),
        (2, B, "Copy"),
        (3, T, "deep"),
        (1, T, "tail"),
    ])


def test_subtree_end_is_the_last_descendant():
    snap = sample()
    a = snap.find(name="a")
    assert snap.subtree_end(a) == 3
    b = snap.find(name="b")
    assert snap.subtree_end(b) == 6


def test_a_leaf_is_its_own_subtree():
    snap = sample()
    tail = snap.find(name="tail")
    assert snap.subtree_end(tail) == tail.index
    assert snap.subtree(tail) == []


def test_under_scopes_by_ancestor_name():
    snap = sample()
    copy = snap.find(name="Copy")
    assert copy.under("b")
    assert not copy.under("a")
    assert snap.find(name="deep").under("b")


def test_find_all_under_is_bounded_by_the_subtree():
    snap = sample()
    a = snap.find(name="a")
    assert [n.label for n in snap.find_all(type=T, under=a)] == ["a1", "a2"]


def test_parent_links_come_from_the_depth_column():
    snap = sample()
    deep = snap.find(name="deep")
    assert [p.label for p in deep.ancestors()] == ["Copy", "b", "root"]


def test_block_ancestor_skips_nested_text():
    snap = build([
        (0, G, "block"),
        (1, T, "outer"),
        (2, T, "inner"),
    ])
    inner = snap.find(name="inner")
    assert inner.block_ancestor().label == "block"


def test_matching_is_trimmed_and_case_insensitive():
    snap = build([(0, G, "  Chat Messages  ")])
    assert snap.find(name="chat messages") is not None
