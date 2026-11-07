import copy
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from word_atlas_anki import publication as pub
from word_atlas_anki.constants import ROOT
from word_atlas_anki.manifest import seal
from word_atlas_anki.util import BuildError, file_digest, write_json
from word_atlas_anki.verify import verify_dist


def asset(path):
    return {"name": path.name, "state": "uploaded", "size": path.stat().st_size, "digest": "sha256:" + file_digest(path)}


def assets(directory):
    return [asset(path) for path in sorted(directory.iterdir()) if path.is_file()]


def release_files(directory, checksum_directory, *, legacy=False):
    paths = {path.name: path for path in directory.iterdir() if path.is_file()}
    if not legacy:
        paths = {name: path for name, path in paths.items() if name.endswith(".apkg") or name == "manifest.json"}
        checksum_directory.mkdir(parents=True, exist_ok=True)
        checksum_path = checksum_directory / "SHA256SUMS"
        checksum_path.write_text("".join(f"{file_digest(paths[name])}  {name}\n" for name in sorted(paths)), encoding="utf-8")
        paths["SHA256SUMS"] = checksum_path
    return paths


def test_baseline_includes_prereleases_but_not_drafts():
    old = {"id": 1, "draft": False, "prerelease": False, "published_at": "2026-01-01"}
    new = {"id": 2, "draft": False, "prerelease": True, "published_at": "2026-02-01"}
    draft = {"id": 3, "draft": True, "published_at": None}
    assert pub.latest_published([old, draft, new]) is new
    assert pub.latest_published([draft]) is None


def test_unreadable_baseline_is_not_first_release(monkeypatch, tmp_path):
    existing = {"tag_name": "wa-test", "assets": [{"name": "manifest.json"}, {"name": "SHA256SUMS"}]}
    monkeypatch.setattr(pub, "gh", lambda *a, **kw: (_ for _ in ()).throw(BuildError("download failed")))
    with pytest.raises(BuildError, match="download failed"):
        pub.baseline("owner/repo", existing, tmp_path)
    with pytest.raises(BuildError, match="lacks a valid baseline"):
        pub.baseline("owner/repo", {"assets": []}, tmp_path)
    assert pub.baseline("owner/repo", None, tmp_path) is None


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("problem", [None, "missing", "duplicate", "unexpected", "missing-checksum",
                                     "unexpected-checksum", "manifest-digest", "package-digest", "tag"])
def test_baseline_accepts_only_verified_recognized_asset_sets(monkeypatch, built, tmp_path, legacy, problem):
    out, original, _ = built
    manifest = copy.deepcopy(original)
    manifest.update(fixture=False, builder_sha="a" * 40)
    source = tmp_path / "source"
    source.mkdir()
    write_json(source / "manifest.json", seal(manifest))
    checksums = pub.read_checksums(out)
    checksums["manifest.json"] = file_digest(source / "manifest.json")
    if not legacy:
        for name in ("changes.json", "release-notes.md"):
            del checksums[name]
    release = {"tag_name": "wa-" + manifest["input_fingerprint"], "draft": False,
               "assets": [{"name": name} for name in sorted(set(checksums) | {"SHA256SUMS"})]}
    package = manifest["packages"][0]["name"]
    if problem == "missing":
        release["assets"] = [record for record in release["assets"] if record["name"] != package]
    elif problem == "duplicate":
        release["assets"].append({"name": package})
    elif problem == "unexpected":
        release["assets"].append({"name": "extra.txt"})
    elif problem == "missing-checksum":
        del checksums[package]
    elif problem == "unexpected-checksum":
        checksums["extra.txt"] = "0" * 64
        release["assets"].append({"name": "extra.txt"})
    elif problem == "manifest-digest":
        checksums["manifest.json"] = "0" * 64
    elif problem == "package-digest":
        checksums[package] = "0" * 64
    elif problem == "tag":
        release["tag_name"] = "wa-" + "0" * 64
    (source / "SHA256SUMS").write_text("".join(f"{checksum}  {name}\n" for name, checksum in sorted(checksums.items())), encoding="utf-8")
    downloads = []

    def download(*args, **kwargs):
        assert args[:3] == ("release", "download", release["tag_name"])
        assert [args[i + 1] for i, arg in enumerate(args) if arg == "--pattern"] == ["manifest.json", "SHA256SUMS"]
        destination = Path(args[args.index("--dir") + 1])
        for name in ("manifest.json", "SHA256SUMS"):
            (destination / name).write_bytes((source / name).read_bytes())
            downloads.append(name)
        return ""

    monkeypatch.setattr(pub, "gh", download)
    if problem:
        with pytest.raises(BuildError, match="Published baseline"):
            pub.baseline("owner/repo", release, tmp_path / "baseline")
    else:
        assert pub.baseline("owner/repo", release, tmp_path / "baseline") == manifest
    assert downloads == ["manifest.json", "SHA256SUMS"]


def test_preflight_skips_unchanged_inputs_without_fetching_media(monkeypatch, built, tmp_path):
    _, manifest, entries = built
    release = {"id": 1, "draft": False, "published_at": "2026-10-27", "tag_name": "wa-" + manifest["input_fingerprint"]}
    monkeypatch.setattr(pub, "check_repo", lambda _: {})
    monkeypatch.setattr(pub, "current_builder", lambda *a: "a" * 40)
    monkeypatch.setattr(pub, "read_json_policy", lambda _: {"channel": "prerelease"})
    monkeypatch.setattr(pub, "releases", lambda _: [release])
    monkeypatch.setattr(pub, "baseline", lambda *a: manifest)
    monkeypatch.setattr(pub, "checkout_metadata", lambda *a: manifest["upstream_sha"])
    monkeypatch.setattr(pub, "load_dataset", lambda _: SimpleNamespace(entries=entries))
    monkeypatch.setattr(pub, "load_identities", lambda *a: ({entry["id"]: entry["id"] for entry in entries}, set()))
    monkeypatch.setattr(pub, "input_fingerprint", lambda *a: {"fingerprint": manifest["input_fingerprint"]})
    monkeypatch.setattr(pub, "hydrate_media", lambda *a, **kw: pytest.fail("Unchanged inputs must not fetch media"))
    state = pub.check_release("owner/repo", tmp_path / "upstream", tmp_path / "state.json")
    assert state["needs_update"] is False
    assert state["previous_tag"] == release["tag_name"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("problem", ["missing", "duplicate", "unexpected", "bytes", "digest", "uploading"])
def test_attachment_gate_rejects_incomplete_or_corrupt_assets(built, tmp_path, legacy, problem):
    out, _, _ = built
    paths = release_files(out, tmp_path, legacy=legacy)
    release = {"tag_name": "wa-test", "assets": [asset(path) for path in paths.values()]}
    pub.verify_remote_assets("owner/repo", release, paths)
    if problem == "missing":
        release["assets"].pop()
    elif problem == "duplicate":
        release["assets"].append(release["assets"][0])
    elif problem == "unexpected":
        release["assets"][0]["name"] = "extra.txt"
    elif problem == "bytes":
        release["assets"][0]["size"] += 1
    elif problem == "digest":
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
    else:
        release["assets"][0]["state"] = "starter"
    with pytest.raises(BuildError):
        pub.verify_remote_assets("owner/repo", release, paths)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("corrupt", [False, True])
def test_attachment_gate_downloads_assets_without_digest(monkeypatch, built, tmp_path, legacy, corrupt):
    out, _, _ = built
    paths = release_files(out, tmp_path, legacy=legacy)
    release = {"tag_name": "wa-test", "assets": [asset(path) for path in paths.values()]}
    for record in release["assets"]:
        record.pop("digest")
    downloads = []

    def download(*args, **kwargs):
        assert args[:3] == ("release", "download", "wa-test")
        name = args[args.index("--pattern") + 1]
        destination = Path(args[args.index("--dir") + 1])
        content = paths[name].read_bytes()
        if corrupt:
            content = bytes([content[0] ^ 1]) + content[1:]
        (destination / name).write_bytes(content)
        downloads.append(name)
        return ""

    monkeypatch.setattr(pub, "gh", download)
    if corrupt:
        with pytest.raises(BuildError, match="Downloaded asset checksum mismatch"):
            pub.verify_remote_assets("owner/repo", release, paths)
        assert len(downloads) == 1
    else:
        pub.verify_remote_assets("owner/repo", release, paths)
        assert set(downloads) == set(paths)


@pytest.mark.parametrize("package_count", [1, 2])
def test_publication_assets_keep_local_files_and_filter_checksums(tmp_path, package_count):
    out = tmp_path / "dist"
    out.mkdir()
    names = [f"deck-{number}.apkg" for number in range(package_count)]
    for name in [*names, "manifest.json", "changes.json", "release-notes.md"]:
        (out / name).write_text(name, encoding="utf-8")
    (out / "SHA256SUMS").write_text("".join(f"{file_digest(path)}  {path.name}\n" for path in sorted(out.iterdir())), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in out.iterdir()}
    with pub.publication_assets(out, {"packages": [{"name": name} for name in names]}) as paths:
        assert set(paths) == set(names) | {"manifest.json", "SHA256SUMS"}
        assert all(path == out / name for name, path in paths.items() if name != "SHA256SUMS")
        checksum_path = paths["SHA256SUMS"]
        assert checksum_path.parent != out
        assert pub.read_checksums(checksum_path.parent) == {name: file_digest(paths[name]) for name in paths if name != "SHA256SUMS"}
        assert {path.name: path.read_bytes() for path in out.iterdir()} == before
    assert not checksum_path.exists()
    assert {path.name: path.read_bytes() for path in out.iterdir()} == before


def mocked_publisher(monkeypatch, built, tmp_path, *, published=False, legacy=False, lose_asset=False):
    out, original, _ = built
    manifest = copy.deepcopy(original)
    manifest["builder_sha"] = "a" * 40
    manifest["fixture"] = False
    state = {key: manifest[key] for key in ("builder_sha", "upstream_sha", "input_fingerprint", "channel")}
    state.update(repo="owner/repo", needs_update=True, previous_tag=None)
    state_path = tmp_path / "state.json"
    write_json(state_path, state)
    release = {"id": 7, "tag_name": "wa-" + manifest["input_fingerprint"], "draft": not published,
               "prerelease": True, "published_at": "2026-10-26" if published else None,
               "html_url": "https://github.com/owner/repo/releases/tag/test",
               "assets": [asset(path) for path in release_files(out, tmp_path / "published", legacy=legacy).values()] if published else []}
    calls = []
    monkeypatch.setattr(pub, "check_repo", lambda _: {})
    monkeypatch.setattr(pub, "current_builder", lambda *a: manifest["builder_sha"])
    monkeypatch.setattr(pub, "read_json_policy", lambda _: {"channel": manifest["channel"], "client_evidence": manifest["client_evidence"]})
    monkeypatch.setattr(pub, "verify_dist", lambda *a, **kw: manifest)
    monkeypatch.setattr(pub, "local_inputs", lambda _: manifest["input_files"])
    monkeypatch.setattr(pub, "run", lambda *a, **kw: "")
    monkeypatch.setattr(pub, "releases", lambda _: [release])

    def api(endpoint, *, method=None, payload=None):
        if method:
            calls.append((method, copy.deepcopy(payload)))
            if payload.get("draft") is False:
                assert {record["name"] for record in release["assets"]} == {part["name"] for part in manifest["packages"]} | {"manifest.json", "SHA256SUMS"}
                release["draft"] = False
        return copy.deepcopy(release)

    def gh(*args, **kwargs):
        assert args[:2] == ("release", "upload")
        assert release["draft"]
        assert kwargs["retry"] is False
        path = Path(args[3])
        name = path.name
        if not lose_asset or name != "manifest.json":
            release["assets"] = [record for record in release["assets"] if record["name"] != name]
            release["assets"].append(asset(path))
        if name == "SHA256SUMS":
            assert path != out / name
            assert pub.read_checksums(path.parent) == {part["name"]: part["sha256"] for part in manifest["packages"]} | {"manifest.json": file_digest(out / "manifest.json")}
        calls.append(("upload", name))
        return ""

    monkeypatch.setattr(pub, "api", api)
    monkeypatch.setattr(pub, "gh", gh)
    return out, state_path, release, calls


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("download", [False, True])
def test_existing_public_release_is_never_overwritten(monkeypatch, built, tmp_path, legacy, download):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path, published=True, legacy=legacy)
    before = assets(out)
    if download:
        paths = release_files(out, tmp_path / "download", legacy=legacy)
        for record in release["assets"]:
            record.pop("digest")

        def gh(*args, **kwargs):
            assert args[:3] == ("release", "download", release["tag_name"])
            name = args[args.index("--pattern") + 1]
            destination = Path(args[args.index("--dir") + 1])
            (destination / name).write_bytes(paths[name].read_bytes())
            return ""

        monkeypatch.setattr(pub, "gh", gh)
    assert pub.publish_release("owner/repo", out, state) == release["html_url"]
    assert calls == []
    assert assets(out) == before


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("problem", ["missing", "duplicate", "unexpected", "partial-legacy", "bytes", "digest", "uploading", "checksums"])
def test_public_retry_rejects_unrecognized_or_corrupt_assets_without_writes(monkeypatch, built, tmp_path, legacy, problem):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path, published=True, legacy=legacy)
    if problem == "missing":
        release["assets"].pop()
    elif problem == "duplicate":
        release["assets"].append(release["assets"][0])
    elif problem == "unexpected":
        release["assets"].append({**release["assets"][0], "name": "extra.txt"})
    elif problem == "partial-legacy":
        if legacy:
            release["assets"] = [record for record in release["assets"] if record["name"] != "changes.json"]
        else:
            release["assets"].append(asset(out / "changes.json"))
    elif problem == "bytes":
        release["assets"][0]["size"] += 1
    elif problem == "digest":
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
    elif problem == "uploading":
        release["assets"][0]["state"] = "starter"
    else:
        opposite = release_files(out, tmp_path / "opposite", legacy=not legacy)
        release["assets"] = [record for record in release["assets"] if record["name"] != "SHA256SUMS"]
        release["assets"].append(asset(opposite["SHA256SUMS"]))
    with pytest.raises(BuildError, match="Uploaded"):
        pub.publish_release("owner/repo", out, state)
    assert calls == []


@pytest.mark.parametrize("new_draft", [False, True])
def test_draft_promotes_only_after_every_verified_upload(monkeypatch, built, tmp_path, new_draft):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    if new_draft:
        monkeypatch.setattr(pub, "releases", lambda _: [])
    before = assets(out)
    pub.publish_release("owner/repo", out, state)
    assert {name for kind, name in calls if kind == "upload"} == {part["name"] for part in built[1]["packages"]} | {"manifest.json", "SHA256SUMS"}
    assert calls[-1][0] == "PATCH"
    assert calls[-1][1]["draft"] is False
    assert calls[-1][1]["prerelease"] is True
    assert calls[-1][1]["make_latest"] == "false"
    assert calls[-1][1]["body"] == (out / "release-notes.md").read_text(encoding="utf-8")
    assert all(payload.get("draft") is not True for kind, payload in calls if kind == "PATCH")
    assert release["draft"] is False
    assert assets(out) == before
    assert verify_dist(out) == built[1]


@pytest.mark.parametrize("extra", ["changes.json", "release-notes.md", "legacy", "unexpected.txt"])
def test_draft_with_leftover_assets_is_not_promoted(monkeypatch, built, tmp_path, extra):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    if extra == "legacy":
        release["assets"] = assets(out)
    else:
        release["assets"] = [{**asset(out / "changes.json"), "name": extra}]
    before = assets(out)
    with pytest.raises(BuildError, match="attachment set"):
        pub.publish_release("owner/repo", out, state)
    assert release["draft"] is True
    assert all(kind == "upload" for kind, _ in calls)
    assert {name for kind, name in calls if kind == "upload"} == {part["name"] for part in built[1]["packages"]} | {"manifest.json", "SHA256SUMS"}
    assert assets(out) == before


def test_failed_upload_keeps_release_draft(monkeypatch, built, tmp_path):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path, lose_asset=True)
    with pytest.raises(BuildError, match="attachment set"):
        pub.publish_release("owner/repo", out, state)
    assert release["draft"] is True
    assert not any(kind == "PATCH" and payload.get("draft") is False for kind, payload in calls if isinstance(payload, dict))


def test_upload_retry_cannot_overwrite_newly_public_release(monkeypatch, built, tmp_path):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    attempts = []

    def interrupted_upload(*args, **kwargs):
        attempts.append(args)
        assert kwargs["retry"] is False
        release["draft"] = False
        raise BuildError("Connection lost after upload")

    monkeypatch.setattr(pub, "gh", interrupted_upload)
    monkeypatch.setattr(pub.time, "sleep", lambda _: None)
    with pytest.raises(BuildError, match="made public"):
        pub.publish_release("owner/repo", out, state)
    assert len(attempts) == 1
    assert calls == []


def test_reused_draft_is_not_unpublished_if_someone_publishes_it(monkeypatch, built, tmp_path):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    real_api = pub.api

    def published_before_upload(endpoint, *, method=None, payload=None):
        if method is None:
            release["draft"] = False
        return real_api(endpoint, method=method, payload=payload)

    monkeypatch.setattr(pub, "api", published_before_upload)
    with pytest.raises(BuildError, match="made public"):
        pub.publish_release("owner/repo", out, state)
    assert calls == []


def test_draft_made_public_after_uploads_is_not_patched(monkeypatch, built, tmp_path):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    real_api = pub.api
    expected = {part["name"] for part in built[1]["packages"]} | {"manifest.json", "SHA256SUMS"}

    def published_after_uploads(endpoint, *, method=None, payload=None):
        if {record["name"] for record in release["assets"]} == expected:
            release["draft"] = False
        return real_api(endpoint, method=method, payload=payload)

    monkeypatch.setattr(pub, "api", published_after_uploads)
    with pytest.raises(BuildError, match="Draft changed while validating"):
        pub.publish_release("owner/repo", out, state)
    assert all(kind == "upload" for kind, _ in calls)


@pytest.mark.parametrize("problem", [None, "legacy", "unexpected", "digest"])
def test_draft_made_public_before_promotion_is_only_verified(monkeypatch, built, tmp_path, problem):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    real_builder = pub.current_builder
    builder_checks = []

    def published_before_promotion(*args):
        builder_checks.append(args)
        if len(builder_checks) == 2:
            release["draft"] = False
            if problem == "legacy":
                release["assets"] = assets(out)
            elif problem == "unexpected":
                release["assets"].append({**release["assets"][0], "name": "extra.txt"})
            elif problem == "digest":
                release["assets"][0]["digest"] = "sha256:" + "0" * 64
        return real_builder(*args)

    monkeypatch.setattr(pub, "current_builder", published_before_promotion)
    if problem:
        with pytest.raises(BuildError, match="Uploaded"):
            pub.publish_release("owner/repo", out, state)
    else:
        assert pub.publish_release("owner/repo", out, state) == release["html_url"]
    assert len(builder_checks) == 2
    assert all(kind == "upload" for kind, _ in calls)


def test_publication_stops_if_another_version_was_published(monkeypatch, built, tmp_path):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    other = {**release, "id": 8, "tag_name": "wa-" + "0" * 64, "draft": False, "published_at": "2026-11-07"}
    monkeypatch.setattr(pub, "releases", lambda _: [release, other])
    with pytest.raises(BuildError, match="Another version was published"):
        pub.publish_release("owner/repo", out, state)
    assert calls == []


def test_workflow_permissions_actions_and_default_branch():
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    assert release["name"] == "CI and Release"
    assert not (ROOT / ".github/workflows/ci.yml").exists()
    assert set(release["jobs"]) == {"test", "publish", "cleanup"}
    assert release["permissions"] == {"contents": "read"}
    for name, job in release["jobs"].items():
        permissions = {"publish": {"contents": "write"}, "cleanup": {"actions": "write"}}
        assert job.get("permissions", release["permissions"]) == permissions.get(name, {"contents": "read"})
        assert not job.get("continue-on-error")
        for step in job["steps"]:
            assert not step.get("continue-on-error")
            if "uses" in step:
                assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
            if step.get("uses", "").startswith("actions/upload-artifact@"):
                assert step["with"]["retention-days"] == 7
    # PyYAML's YAML 1.1 loader treats the unquoted key "on" as True.
    triggers = release.get("on", release.get(True))
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch", "schedule"}
    assert triggers["push"] == {"branches": ["**"]}
    assert triggers["pull_request"] is None
    assert triggers["schedule"] == [{"cron": "23 3 */3 * *"}]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"cleanup_dry_run"}
    assert inputs["cleanup_dry_run"]["type"] == "boolean"
    assert inputs["cleanup_dry_run"]["default"] is True
    assert "release still runs" in inputs["cleanup_dry_run"]["description"]
    default_ref = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    trusted_branch = "github.event_name != 'pull_request' && " + default_ref
    validation = "steps.scope.outputs.test_required == 'true' || steps.check.outputs.needs_update == 'true'"
    setup = "steps.scope.outputs.test_required == 'true' || (" + trusted_branch + ")"
    test = release["jobs"]["test"]
    assert test["if"] == "github.event.deleted != true"
    assert test["timeout-minutes"] == "${{ " + default_ref + " && 120 || 30 }}"
    assert test["outputs"] == {"needs_update": "${{ steps.check.outputs.needs_update }}", "builder_sha": "${{ steps.check.outputs.builder_sha }}"}
    steps = {step["name"]: step for step in test["steps"] if "name" in step}
    ids = {step["id"]: step for step in test["steps"] if "id" in step}
    assert test["steps"][0]["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 0, "persist-credentials": False}
    assert next(step for step in test["steps"] if step.get("uses", "").startswith("actions/setup-python@"))["if"] == setup
    assert steps["Install verified builder"]["if"] == setup
    assert ids["check"]["if"] == trusted_branch
    assert ids["check"]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert steps["Install validation tools"]["if"] == validation
    assert ids["tests"]["if"] == validation
    assert ids["tests"]["run"].splitlines() == ["node --check templates/card.js", "python -m pytest --junitxml=test-results/pytest.xml"]
    assert sum("python -m pytest --junitxml=" in step.get("run", "") for job in release["jobs"].values() for step in job["steps"]) == 1
    for name in ("Build full offline deck from the checked snapshot", "Test real full-deck layouts", "Transfer complete candidate", "Transfer preflight state"):
        assert steps[name]["if"] == "steps.check.outputs.needs_update == 'true'"
        assert test["steps"].index(steps[name]) > test["steps"].index(ids["tests"])
    assert "verify --out dist --release --native" in steps["Build full offline deck from the checked snapshot"]["run"]
    assert steps["Test real full-deck layouts"]["env"] == {"WA_REAL_DIST": "dist"}
    assert steps["Keep test and build diagnostics"]["if"] == "always() && (steps.tests.outcome != 'skipped' || failure())"
    publish = release["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "write"}
    assert publish["needs"] == "test"
    assert publish["if"] == trusted_branch + " && needs.test.outputs.needs_update == 'true'"
    assert publish["steps"][0]["with"]["ref"] == "${{ needs.test.outputs.builder_sha }}"
    assert release["concurrency"] == {
        "group": "${{ " + default_ref + " && 'word-atlas-release' || format('ci-{0}', github.ref) }}",
        "cancel-in-progress": "${{ github.ref != format('refs/heads/{0}', github.event.repository.default_branch) }}",
    }
    cleanup = release["jobs"]["cleanup"]
    assert cleanup["if"] == trusted_branch + " && github.event.deleted != true"
    assert "needs" not in cleanup
    assert cleanup["timeout-minutes"] == 10
    assert len(cleanup["steps"]) == 1
    step = cleanup["steps"][0]
    assert step["uses"] == "actions/github-script@ed597411d8f924073f98dfc5c65a23a2325f34cd"
    assert not step.get("continue-on-error")
    assert "if" not in step and "run" not in step
    assert step["env"]["CLEANUP_DRY_RUN"] == "${{ github.event_name == 'workflow_dispatch' && inputs.cleanup_dry_run }}"
    assert step["with"]["retries"] == 3
    assert step["with"]["retry-exempt-status-codes"] == "400,401,403,404,422"
    assert "${{" not in step["with"]["script"]
    assert not (ROOT / ".github/workflows/cleanup.yml").exists()


@pytest.fixture
def workflow_scope(monkeypatch, tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["test"]["steps"] if step.get("id") == "scope")
    lines = step["run"].splitlines()
    assert lines[0] == "python3 - <<'PY'" and lines[-1] == "PY"
    script = "\n".join(lines[1:-1])

    def run(event_name, paths=b"", *, base="a" * 40, head="b" * 40, git_error=False):
        event = {"before": base} if event_name == "push" else {"pull_request": {"base": {"sha": base}}}
        event_path, output = tmp_path / "event.json", tmp_path / "output"
        write_json(event_path, event)
        output.write_text("", encoding="utf-8")
        for key, value in {"GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(event_path),
                           "GITHUB_OUTPUT": str(output), "GITHUB_SHA": head}.items():
            monkeypatch.setenv(key, value)
        calls = []

        def diff(args, **kwargs):
            assert args == ["git", "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", base, head, "--"]
            assert kwargs == {"stderr": subprocess.DEVNULL}
            calls.append(args)
            if git_error:
                raise subprocess.CalledProcessError(128, args)
            return paths

        monkeypatch.setattr(subprocess, "check_output", diff)
        exec(compile(script, "release.yml:scope", "exec"), {})
        result = output.read_text(encoding="utf-8").strip()
        assert result in ("test_required=true", "test_required=false")
        return result == "test_required=true", calls

    return run


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("paths,expected", [
    (b"", False),
    (b"README.md\0docs/preview/back.png\0", False),
    (b"docs/name\nwith-newline.md\0", False),
    (b"src/word_atlas_anki/builder.py\0", True),
    (b"docs/moved.py\0src/removed.py\0", True),
    (b"tests/test_data.py\0", True),
    (b".github/workflows/release.yml\0", True),
    (b".github/ISSUE_TEMPLATE/bug_report.yml\0", True),
    (b"licenses/Word-Atlas-content.txt\0", True),
    (b"config/release-policy.json\0", True),
    (b"requirements.lock\0", True),
])
def test_workflow_scope_only_skips_documentation(workflow_scope, event, paths, expected):
    required, calls = workflow_scope(event, paths)
    assert required is expected
    assert len(calls) == 1


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("base", [None, "0" * 40, "--no-index"])
def test_workflow_scope_unknown_base_requires_checks(workflow_scope, event, base):
    required, calls = workflow_scope(event, b"README.md\0", base=base)
    assert required is True
    assert calls == []


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_workflow_scope_diff_failure_requires_checks(workflow_scope, event):
    required, calls = workflow_scope(event, git_error=True)
    assert required is True
    assert len(calls) == 1


@pytest.mark.parametrize("event,expected", [("workflow_dispatch", True), ("schedule", False)])
def test_workflow_scope_manual_and_scheduled_checks(workflow_scope, event, expected):
    required, calls = workflow_scope(event)
    assert required is expected
    assert calls == []


@pytest.fixture
def run_cleanup():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    script = workflow["jobs"]["cleanup"]["steps"][0]["with"]["script"]
    harness = r"""
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const result = {calls: [], errors: [], logs: [], summary: [], clockReads: 0};
const now = Date.parse('2026-11-08T03:23:00Z');
Date.now = () => now + result.clockReads++ * 24 * 60 * 60 * 1000;
process.env.CLEANUP_DRY_RUN = String(input.dry_run ?? false);
const context = {repo: {owner: 'owner', repo: 'repo'}, runId: 999999};
const listed = new Map(input.runs.map(run => [String(run.id), run]));
const checked = new Set();
let listedAll = false;
function apiError(stage, id) {
  if (input.error?.stage === stage && (id === undefined || String(id) === '2')) {
    throw Object.assign(new Error(`${stage} API error ${input.error.status}`), {status: input.error.status});
  }
}
function paramsForRun(params) {
  assert.deepEqual(params, {...context.repo, run_id: String(params.run_id)});
  assert.ok(listedAll, 'Pagination must finish before rechecks or deletes');
}
const actions = {
  listWorkflowRunsForRepo() { throw new Error('Use paginate'); },
  async getWorkflowRun(params) {
    paramsForRun(params);
    result.calls.push(['get', params.run_id]);
    apiError('get', params.run_id);
    checked.add(params.run_id);
    return {data: input.rechecks?.[params.run_id] ?? listed.get(params.run_id)};
  },
  async deleteWorkflowRun(params) {
    paramsForRun(params);
    assert.ok(checked.has(params.run_id), 'Every delete must have a recheck');
    result.calls.push(['delete', params.run_id]);
    apiError('delete', params.run_id);
    return {status: 204};
  }
};
const github = {
  rest: {actions},
  async paginate(method, params) {
    assert.equal(method, actions.listWorkflowRunsForRepo);
    assert.deepEqual(params, {...context.repo, per_page: 100});
    result.calls.push(['list']);
    apiError('list');
    const runs = [];
    for (let offset = 0; offset < input.runs.length; offset += params.per_page) {
      result.calls.push(['page', offset / params.per_page + 1]);
      runs.push(...input.runs.slice(offset, offset + params.per_page));
      await Promise.resolve();
    }
    listedAll = true;
    return runs;
  }
};
const core = {
  info(message) { result.logs.push(message); },
  setFailed(message) { result.errors.push(message); },
  summary: {
    addHeading(value) { result.summary.push(value); return this; },
    addRaw(value) { result.summary.push(value); return this; },
    addTable(value) { result.counts = Object.fromEntries(value.slice(1).map(([k, v]) => [k, Number(v)])); return this; },
    async write() { result.summaryWritten = true; }
  }
};
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
new AsyncFunction('github', 'core', 'context', input.script)(github, core, context)
  .then(() => process.stdout.write(JSON.stringify(result)))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""

    def invoke(runs, **scenario):
        completed = subprocess.run(["node", "-e", harness], input=json.dumps({"script": script, "runs": runs, **scenario}),
                                   capture_output=True, text=True, timeout=20)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["clockReads"] == 1
        assert result["summaryWritten"] is True
        assert "2026-11-01T03:23:00.000Z" in result["summary"][1]
        assert set(result["counts"]) == {"scanned", "candidates", "deleted", "would_delete", "skipped", "failed"}
        return result

    return invoke


def cleanup_run(run_id, **updates):
    return {"id": run_id, "status": "completed", "conclusion": "success",
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z", **updates}


def test_cleanup_unfiltered_pagination_dedup_and_two_phase(run_cleanup):
    runs = [cleanup_run(i) for i in range(1, 1106)]
    runs.extend([runs[0], cleanup_run("1"), runs[-1]])
    result = run_cleanup(runs)
    assert result["errors"] == []
    assert result["counts"] == {"scanned": 1108, "candidates": 1105, "deleted": 1105,
                                "would_delete": 0, "skipped": 3, "failed": 0}
    assert result["calls"][:13] == [["list"], *[["page", i] for i in range(1, 13)]]
    assert result["calls"][13:] == [[method, str(i)] for i in range(1, 1106) for method in ("get", "delete")]


@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_strict_cutoff_conclusions_and_exclusions(run_cleanup, dry_run):
    conclusions = ["success", "failure", "neutral", "cancelled", "skipped", "timed_out",
                   "action_required", "stale", "startup_failure", None]
    eligible = [cleanup_run(i, conclusion=conclusion) for i, conclusion in enumerate(conclusions, 1)]
    eligible.append(cleanup_run(11, created_at="2026-11-01T03:22:59.999Z", updated_at="2026-11-01T03:22:59.999Z"))
    excluded = [cleanup_run(999999)]
    for status in ("queued", "in_progress", "requested", "waiting", "pending"):
        excluded.append(cleanup_run(20 + len(excluded), status=status))
    for field in ("created_at", "updated_at"):
        for value in ("2026-11-01T03:23:00Z", "2026-11-01T03:23:00.001Z", "2026-11-08T00:00:00Z", None, "invalid"):
            excluded.append(cleanup_run(20 + len(excluded), **{field: value}))
    result = run_cleanup(eligible + excluded, dry_run=dry_run)
    assert result["errors"] == []
    assert result["counts"] == {"scanned": 27, "candidates": 11, "deleted": 0 if dry_run else 11,
                                "would_delete": 11 if dry_run else 0, "skipped": 16, "failed": 0}
    assert [call[1] for call in result["calls"] if call[0] == "get"] == [str(i) for i in range(1, 12)]
    assert [call[1] for call in result["calls"] if call[0] == "delete"] == ([] if dry_run else [str(i) for i in range(1, 12)])
    assert f"dry-run: {str(dry_run).lower()}" in result["summary"][1]


@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_rechecks_changed_eligibility(run_cleanup, dry_run):
    runs = [cleanup_run(i) for i in range(1, 7)]
    rechecks = {"1": cleanup_run(1, status="in_progress"), "2": cleanup_run(2, status="queued"),
                "3": cleanup_run(3, updated_at="2026-11-08T03:00:00Z"),
                "4": cleanup_run(4, created_at="2026-11-01T03:23:00Z"),
                "5": cleanup_run(5, updated_at="2026-11-01T03:23:00Z")}
    result = run_cleanup(runs, rechecks=rechecks, dry_run=dry_run)
    assert result["errors"] == []
    assert result["counts"] == {"scanned": 6, "candidates": 6, "deleted": 0 if dry_run else 1,
                                "would_delete": 1 if dry_run else 0, "skipped": 5, "failed": 0}
    assert [call for call in result["calls"] if call[0] == "get"] == [["get", str(i)] for i in range(1, 7)]
    assert [call for call in result["calls"] if call[0] == "delete"] == ([] if dry_run else [["delete", "6"]])


@pytest.mark.parametrize("stage,dry_run", [("list", False), ("get", False), ("delete", False), ("get", True)])
@pytest.mark.parametrize("status", [403, 404, 500])
def test_cleanup_api_errors_fail_visibly(run_cleanup, stage, dry_run, status):
    result = run_cleanup([cleanup_run(1), cleanup_run(2), cleanup_run(3)],
                         dry_run=dry_run, error={"stage": stage, "status": status})
    assert result["errors"] == [f"{stage} API error {status}"]
    assert result["counts"]["failed"] == 1
    assert result["counts"]["deleted"] == (1 if stage != "list" and not dry_run else 0)
    assert result["counts"]["scanned"] == (0 if stage == "list" else 3)
    assert ["get", "3"] not in result["calls"]
    if dry_run:
        assert not any(call[0] == "delete" for call in result["calls"])


@pytest.mark.parametrize("filename,name,title,fields", [
    ("bug_report.yml", "Bug Report", "[BUG] ", [
        ("actual-result", "textarea", "What went wrong?", True),
        ("reproduction", "textarea", "Steps to Reproduce", True),
        ("expected-result", "textarea", "Expected Result", True),
        ("client", "dropdown", "Anki Client", True),
        ("client-version", "input", "Client Version (Optional)", False),
        ("deck-version", "input", "Deck Version (Optional)", False),
        ("operating-system", "input", "Operating System (Optional)", False),
        ("affected-word", "input", "Affected Word (Optional)", False),
        ("screenshots", "textarea", "Screenshots (Optional)", False),
        ("logs", "textarea", "Logs (Optional)", False),
    ]),
    ("feature_request.yml", "Feature Request", "[FEATURE] ", [
        ("problem", "textarea", "Problem or Use Case", True),
        ("proposed-change", "textarea", "Proposed Change", True),
        ("alternatives", "textarea", "Alternatives (Optional)", False),
    ]),
])
def test_issue_template_metadata_and_sections(filename, name, title, fields):
    directory = ROOT / ".github/ISSUE_TEMPLATE"
    text = (directory / filename).read_text(encoding="utf-8")
    assert text.isascii()
    form = yaml.safe_load(text)
    assert set(form) == {"name", "description", "title", "body"}
    assert form["name"] == name
    assert form["title"] == title
    assert all(isinstance(form[key], str) and form[key].strip() and form[key].isascii()
               for key in ("name", "description", "title"))
    body = form["body"]
    assert isinstance(body, list) and len(body) == len(fields)
    assert len({field["id"] for field in body}) == len(body)
    assert len({field["attributes"]["label"] for field in body}) == len(body)
    for field, (field_id, field_type, label, required) in zip(body, fields):
        assert set(field) == {"type", "id", "attributes", "validations"}
        assert field["id"] == field_id
        assert re.fullmatch(r"[A-Za-z0-9_-]+", field["id"])
        assert field["type"] == field_type
        assert field_type in {"textarea", "input", "dropdown"}
        assert set(field["validations"]) == {"required"}
        assert field["validations"]["required"] is required
        attributes = field["attributes"]
        assert attributes["label"] == label
        if field_type == "dropdown":
            assert set(attributes) == {"label", "options"}
            options = attributes["options"]
            assert isinstance(options, list) and options
            assert all(isinstance(option, str) and option.strip() and option.isascii() for option in options)
            assert len(options) == len(set(options))
            assert options == ["Anki Desktop", "AnkiDroid", "AnkiMobile", "AnkiWeb", "Other / build issue"]
        else:
            assert set(attributes) == {"label", "description"}
            assert all(isinstance(value, str) and value.strip() and value.isascii() for value in attributes.values())
        if field_id in {"screenshots", "logs"}:
            assert "Remove personal information before sharing." in attributes["description"]
    assert not list(directory.glob("*.md"))
    config_text = (directory / "config.yml").read_text(encoding="utf-8")
    assert config_text.isascii()
    config = yaml.safe_load(config_text)
    assert set(config) == {"blank_issues_enabled"}
    assert config["blank_issues_enabled"] is False
