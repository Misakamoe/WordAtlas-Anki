import copy
import re
import shutil

import pytest

from conftest import NOW, SHA, write_dataset
from word_atlas_anki.builder import build, read_json_policy
from word_atlas_anki.manifest import load_manifest, seal
from word_atlas_anki.media import MediaStore, decode_media
from word_atlas_anki.release_notes import release_notes
from word_atlas_anki.upstream import check_licenses
from word_atlas_anki.util import BuildError, read_json, write_json
from word_atlas_anki.verify import verify_dist


def test_package_manifest_and_repeat_timestamps(built, upstream, tmp_path):
    first, manifest, _ = built
    root, _ = upstream
    second = tmp_path / "repeat"
    again = build(root, SHA, second, previous_manifest=first / "manifest.json", now=NOW + 50, cache=tmp_path / "cache", verify_upstream=False)
    assert manifest["notes"] == again["notes"]
    assert manifest["model"] == again["model"]
    assert manifest["input_fingerprint"] == again["input_fingerprint"]
    assert manifest["packages"] == again["packages"]
    assert read_json(second / "changes.json")["modified"] == []
    assert verify_dist(second)["note_count"] == 6
    assert (second / "release-notes.md").read_text(encoding="utf-8") == release_notes(again, read_json(second / "changes.json"))


@pytest.mark.parametrize("full,channel", [(True, "stable"), (True, "prerelease"), (False, "stable"), (False, "prerelease")])
@pytest.mark.parametrize("initial", [False, True])
def test_release_notes_keep_updates_brief_and_initial_useful(built, full, channel, initial):
    out, original, _ = built
    manifest = {**original, "full": full, "channel": channel}
    changes = read_json(out / "changes.json")
    changes["initial"] = initial
    notes = release_notes(manifest, changes)
    headings = ["本次变化", "内容与下载"]
    if initial and full:
        headings += ["导入与兼容", "来源与许可"]
        assert "先备份集合与媒体" in notes
        assert "CC BY 4.0" in notes and "非官方" in notes
    assert re.findall(r"^## (.+)$", notes, flags=re.MULTILINE) == headings
    assert not re.search(r"^# ", notes, flags=re.MULTILINE)
    assert ("预发布" in notes) == (channel == "prerelease")
    assert ("不完整小样" in notes) == (not full)
    assert ("不可作为全量更新" in notes) == (not full)
    assert "| 文件 | 词条 | 大小 |" in notes
    assert "README" in notes and "SHA256SUMS" in notes
    assert "changes.json" not in notes and "release-notes.md" not in notes
    assert manifest["input_fingerprint"] not in notes
    for part in manifest["packages"]:
        assert part["name"] in notes
        assert part["sha256"] not in notes


def test_release_notes_bound_examples_and_keep_relevant_changes(built):
    out, manifest, _ = built
    changes = read_json(out / "changes.json")
    changes.update(initial=False, template_changed=True, builder_changed=False,
                   added=[{"word": "[one] | <tag>"}, {"word": "two"}, {"word": "three"}, {"word": "omitted-word"}],
                   modified=[], confirmed_deleted=[{"word": "removed-word"}],
                   field_changes={"Word": 1}, media_changes={"uk": 1, "img": 0}, previous_upstream_sha="b" * 40)
    notes = release_notes(manifest, changes)
    assert "词条：新增 4，修改 0，确认删除 1" in notes
    assert "卡片模板" in notes and "构建流程" not in notes
    assert "词头 1" in notes and "英音单词 1" in notes
    assert "插画 0" not in notes
    assert r"\[one\] \| &lt;tag&gt;" in notes
    assert "omitted-word" not in notes and "removed-word" in notes
    assert "changes.json" not in notes
    assert f'compare/{"b" * 40}...{manifest["upstream_sha"]}' in notes
    changes["media_changes"] = {}
    assert "已有词条媒体无变化" in release_notes(manifest, changes)
    changes["previous_upstream_sha"] = manifest["upstream_sha"]
    assert "/compare/" not in release_notes(manifest, changes)


def test_release_notes_explain_multiple_packages_only_when_needed(built):
    out, original, _ = built
    changes = read_json(out / "changes.json")
    assert "全部分包" not in release_notes(original, changes)
    manifest = copy.deepcopy(original)
    part = manifest["packages"][0]
    manifest["packages"] = [
        {**part, "name": "part-1.apkg", "notes": part["notes"][:3]},
        {**part, "name": "part-2.apkg", "notes": part["notes"][3:]},
    ]
    notes = release_notes(manifest, changes)
    assert "请导入全部分包" in notes
    assert "| `part-1.apkg` | 3 |" in notes
    assert "| `part-2.apkg` | 3 |" in notes


def test_sample_explicitly_incomplete(upstream, tmp_path):
    root, _ = upstream
    out = tmp_path / "sample"
    manifest = build(root, SHA, out, sample=2, now=NOW, cache=tmp_path / "cache", verify_upstream=False)
    assert manifest["full"] is False and manifest["note_count"] == 2
    assert "INCOMPLETE-SAMPLE" in manifest["packages"][0]["name"]
    with pytest.raises(BuildError, match="incomplete/fixture"):
        load_manifest(out / "manifest.json", require_full=True)


def test_missing_and_corrupt_media_fail(upstream, tmp_path):
    root, _ = upstream
    source = root / "public/atlas/audio/uk/alpha.mp3"
    source.unlink()
    with pytest.raises(BuildError, match="Missing input"):
        build(root, SHA, tmp_path / "missing", cache=tmp_path / "cache", verify_upstream=False)
    source.write_bytes(b"ID3" + b"\0" * 1000)
    store = MediaStore(tmp_path / "media")
    store.add(source)
    with pytest.raises(BuildError):
        decode_media(store)


def test_unconfirmed_deletion_blocks(upstream, built, tmp_path, project):
    root, values = upstream
    before, _, _ = built
    write_dataset(root, values[1:])
    with pytest.raises(BuildError, match="Unconfirmed deletion"):
        build(root, SHA, tmp_path / "blocked", previous_manifest=before / "manifest.json", project=project, verify_upstream=False)
    write_json(project / "config/identities.json", {"aliases": {}, "confirmed_deletions": ["alpha"]})
    result = build(root, SHA, tmp_path / "confirmed", previous_manifest=before / "manifest.json", project=project, cache=tmp_path / "cache", verify_upstream=False)
    assert result["note_count"] == 5
    assert read_json(tmp_path / "confirmed/changes.json")["confirmed_deleted"][0]["id"] == "alpha"


def test_renamed_id_keeps_guid(upstream, built, tmp_path, project):
    root, values = upstream
    before, original, _ = built
    values[0]["id"] = "alpha-renamed"
    write_dataset(root, values)
    for path in root.glob("public/atlas/**/alpha.*"):
        shutil.copyfile(path, path.with_name("alpha-renamed" + path.suffix))
    write_json(project / "config/identities.json", {"aliases": {"alpha-renamed": "alpha"}, "confirmed_deletions": []})
    after = build(root, SHA, tmp_path / "renamed", previous_manifest=before / "manifest.json", now=NOW + 30, project=project, cache=tmp_path / "cache", verify_upstream=False)
    assert after["notes"]["alpha"]["guid"] == original["notes"]["alpha"]["guid"]
    assert after["notes"]["alpha"]["mod"] == original["notes"]["alpha"]["mod"]


def test_licensing_and_stable_release_gates(upstream, project):
    root, _ = upstream
    (root / "LICENSE").write_text("Changed license", encoding="utf-8")
    with pytest.raises(BuildError, match="licenses changed"):
        check_licenses(root, project)
    write_json(project / "config/release-policy.json", {"channel": "stable", "client_evidence": {"Anki": None, "AnkiDroid": None, "AnkiMobile": None}})
    with pytest.raises(BuildError, match="real-device evidence"):
        read_json_policy(project)


def test_manifest_corruption_never_becomes_first_release(built, tmp_path):
    before, _, _ = built
    damaged = read_json(before / "manifest.json")
    damaged["note_count"] += 1
    path = tmp_path / "damaged.json"
    write_json(path, damaged)
    with pytest.raises(BuildError, match="integrity"):
        load_manifest(path)


@pytest.mark.parametrize("field,value", [("fixture", 0), ("upstream_sha", "main"), ("builder_sha", "local")])
def test_manifest_rejects_invalid_source_metadata(built, tmp_path, field, value):
    out, _, _ = built
    value_manifest = read_json(out / "manifest.json")
    value_manifest[field] = value
    path = tmp_path / "invalid-provenance.json"
    write_json(path, seal(value_manifest))
    with pytest.raises(BuildError):
        load_manifest(path)


def test_uncommitted_build_cannot_become_a_published_baseline(built, tmp_path):
    out, _, _ = built
    value = read_json(out / "manifest.json")
    value.update(fixture=False, builder_sha="uncommitted")
    path = tmp_path / "uncommitted.json"
    write_json(path, seal(value))
    with pytest.raises(BuildError, match="committed builder SHA"):
        load_manifest(path, require_full=True)


@pytest.mark.parametrize("delimiter", ["*/", "</style><script>alert(1)</script>"])
def test_license_notice_cannot_escape_style_comment(upstream, project, tmp_path, delimiter):
    root, _ = upstream
    with (project / "LICENSE").open("a", encoding="utf-8") as stream:
        stream.write(delimiter)
    with pytest.raises(BuildError, match="unsafe CSS/HTML"):
        build(root, SHA, tmp_path / "unsafe-notice", project=project, cache=tmp_path / "cache", verify_upstream=False)
