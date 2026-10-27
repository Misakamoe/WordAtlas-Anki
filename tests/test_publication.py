import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from word_atlas_anki import publication as pub
from word_atlas_anki.constants import ROOT
from word_atlas_anki.util import BuildError, file_digest, write_json


def assets(directory):
    return [{"name": path.name, "state": "uploaded", "size": path.stat().st_size, "digest": "sha256:" + file_digest(path)}
            for path in sorted(directory.iterdir()) if path.is_file()]


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


@pytest.mark.parametrize("problem", ["missing", "duplicate", "bytes", "digest", "uploading"])
def test_attachment_gate_rejects_incomplete_or_corrupt_assets(built, problem):
    out, _, _ = built
    release = {"tag_name": "wa-test", "assets": assets(out)}
    pub.verify_remote_assets("owner/repo", release, out)
    if problem == "missing":
        release["assets"].pop()
    elif problem == "duplicate":
        release["assets"].append(release["assets"][0])
    elif problem == "bytes":
        release["assets"][0]["size"] += 1
    elif problem == "digest":
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
    else:
        release["assets"][0]["state"] = "starter"
    with pytest.raises(BuildError):
        pub.verify_remote_assets("owner/repo", release, out)


def mocked_publisher(monkeypatch, built, tmp_path, *, published=False, lose_asset=False):
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
               "html_url": "https://github.com/owner/repo/releases/tag/test", "assets": assets(out) if published else []}
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
                assert len(release["assets"]) == len(assets(out))
                release["draft"] = False
        return copy.deepcopy(release)

    def gh(*args, **kwargs):
        assert args[:2] == ("release", "upload")
        assert release["draft"]
        assert kwargs["retry"] is False
        name = Path(args[3]).name
        if not lose_asset or name != "manifest.json":
            release["assets"].append(next(record for record in assets(out) if record["name"] == name))
        calls.append(("upload", name))
        return ""

    monkeypatch.setattr(pub, "api", api)
    monkeypatch.setattr(pub, "gh", gh)
    return out, state_path, release, calls


def test_existing_public_release_is_never_overwritten(monkeypatch, built, tmp_path):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path, published=True)
    assert pub.publish_release("owner/repo", out, state) == release["html_url"]
    assert calls == []


def test_draft_promotes_only_after_every_verified_upload(monkeypatch, built, tmp_path):
    out, state, release, calls = mocked_publisher(monkeypatch, built, tmp_path)
    pub.publish_release("owner/repo", out, state)
    assert calls[-1][0] == "PATCH"
    assert calls[-1][1]["draft"] is False
    assert calls[-1][1]["prerelease"] is True
    assert calls[-1][1]["make_latest"] == "false"
    assert all(payload.get("draft") is not True for kind, payload in calls if kind == "PATCH")
    assert release["draft"] is False


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


def test_workflow_permissions_actions_and_default_branch():
    for filename in ("ci.yml", "release.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
        assert workflow["permissions"] == {"contents": "read"}
        for name, job in workflow["jobs"].items():
            if name != "publish":
                assert job.get("permissions", {}).get("contents") != "write"
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    assert "default_branch" in release["jobs"]["build"]["if"]
    assert release["jobs"]["publish"]["permissions"] == {"contents": "write"}
    assert release["concurrency"]["cancel-in-progress"] is False
