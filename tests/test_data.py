import copy
import sys

import pytest

from word_atlas_anki.data import load_dataset, load_identities, valid_id
from word_atlas_anki.render import escape, highlight, render_fields
from word_atlas_anki.util import BuildError, read_json, run, safe_file, write_json
from conftest import write_dataset


def test_complete_data(upstream):
    root, values = upstream
    dataset = load_dataset(root)
    assert len(dataset.entries) == 6
    assert dataset.coverage["ex2"] == 1
    assert dataset.entries[2]["uk"] == ""
    assert dataset.entries[3]["us"] == ""
    assert dataset.entries[0]["note"] == "完整用法提醒。"
    fields = render_fields(dataset.entries[0], "alpha", "教育", {})
    assert "含义一" in fields["Senses"] and "含义二" in fields["Senses"]
    assert "完整用法提醒" in fields["UsageNote"]
    assert fields["IPAUK"] == fields["IPAUS"] and fields["AudioUS"] == ""


@pytest.mark.parametrize("value", ["../escape", "a/b", "a\\b", "A", "", "a" * 65, "a:bad", None, True])
def test_reject_invalid_id(value):
    with pytest.raises(BuildError):
        valid_id(value)


@pytest.mark.parametrize("mutation", ["duplicate", "word", "mapping", "flags", "count", "examples", "unknown", "index-id"])
def test_invalid_structures_fail(upstream, mutation):
    root, values = upstream
    data_path = root / "data/word-atlas.json"
    index_path = root / "public/atlas/index.json"
    data, index = read_json(data_path), read_json(index_path)
    if mutation == "duplicate":
        data.append(copy.deepcopy(data[0]))
    elif mutation == "word":
        index["words"][0][1] = "mismatch"
    elif mutation == "mapping":
        index["words"][0][4] = -1
    elif mutation == "flags":
        index["words"][0][6] = 32
    elif mutation == "count":
        index["count"] += 1
    elif mutation == "examples":
        data[0]["ex"].append(data[0]["ex"][0])
    elif mutation == "unknown":
        data[0]["arbitrary_script"] = "bad"
    elif mutation == "index-id":
        index["words"][0][0] = "unknown"
    write_json(data_path, data)
    write_json(index_path, index)
    with pytest.raises(BuildError):
        load_dataset(root)


def test_paths_are_not_links_or_traversals(tmp_path):
    with pytest.raises(BuildError):
        safe_file(tmp_path, "../outside")
    with pytest.raises(BuildError):
        safe_file(tmp_path, "C:/outside")
    target = tmp_path / "plain.txt"
    target.write_text("x")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("OS does not allow unprivileged symlink creation")
    with pytest.raises(BuildError, match="Links"):
        safe_file(tmp_path, "linked.txt")


def test_safe_escaping_and_inflected_highlight():
    source = '<script>alert("bad")</script> & [sound:bad.mp3]'
    result = escape(source)
    assert "<script>" not in result and "[sound:" not in result
    assert "&lt;script&gt;" in result and "&amp;" in result
    assert highlight("She bought <b>books</b>.", "buy") == "She <mark>bought</mark> &lt;b&gt;books&lt;/b&gt;."
    assert "<mark>libraries</mark>" in highlight("Public libraries are useful.", "library")
    assert "<mark>" not in highlight("Atlas is a book.", "at")


def test_alias_collisions_and_cycles(tmp_path):
    config = tmp_path / "identity.json"
    write_json(config, {"aliases": {"renamed": "alpha"}, "confirmed_deletions": []})
    assert load_identities(config, [{"id": "renamed"}])[0] == {"renamed": "alpha"}
    with pytest.raises(BuildError, match="same canonical"):
        load_identities(config, [{"id": "alpha"}, {"id": "renamed"}])
    write_json(config, {"aliases": {"alpha": "renamed", "renamed": "alpha"}, "confirmed_deletions": []})
    with pytest.raises(BuildError, match="Cyclic"):
        load_identities(config, [{"id": "alpha"}])


def test_subprocess_stdin_preserves_git_line_endings():
    assert run([sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"], input="abc\n") == "6162630a"
