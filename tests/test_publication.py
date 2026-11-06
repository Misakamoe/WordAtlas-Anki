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
            permissions = {"publish": {"contents": "write"}, "cleanup": {"actions": "write"}}
            assert job.get("permissions", workflow["permissions"]) == permissions.get(name, {"contents": "read"})
            assert not job.get("continue-on-error")
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
                if step.get("uses", "").startswith("actions/upload-artifact@"):
                    assert step["with"]["retention-days"] == 7
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 loader treats the unquoted key "on" as True.
    triggers = release.get("on", release.get(True))
    assert set(triggers) == {"push", "workflow_dispatch", "schedule"}
    assert triggers["push"] is None
    assert triggers["schedule"] == [{"cron": "23 3 */3 * *"}]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"cleanup_dry_run"}
    assert inputs["cleanup_dry_run"]["type"] == "boolean"
    assert inputs["cleanup_dry_run"]["default"] is True
    assert "release still runs" in inputs["cleanup_dry_run"]["description"]
    assert release["jobs"]["build"]["if"] == "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    assert release["jobs"]["publish"]["permissions"] == {"contents": "write"}
    assert release["jobs"]["publish"]["needs"] == "build"
    assert release["concurrency"] == {"group": "word-atlas-release", "cancel-in-progress": False}
    cleanup = release["jobs"]["cleanup"]
    assert cleanup["if"] == release["jobs"]["build"]["if"]
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


@pytest.mark.parametrize("filename,sections", [
    ("bug_report.md", ["Problem", "Steps to Reproduce", "Expected and Actual Behavior", "Environment", "Additional Context (Optional)"]),
    ("feature_request.md", ["Problem or Use Case", "Suggested Solution", "Alternatives (Optional)"]),
])
def test_issue_template_metadata_and_sections(filename, sections):
    directory = ROOT / ".github/ISSUE_TEMPLATE"
    text = (directory / filename).read_text(encoding="utf-8")
    assert text.isascii()
    _, front_matter, body = text.split("---", 2)
    metadata = yaml.safe_load(front_matter)
    assert set(metadata) == {"name", "about"}
    assert all(isinstance(value, str) and value.strip() for value in metadata.values())
    assert re.findall(r"^## (.+)$", body, flags=re.MULTILINE) == sections
    if filename == "bug_report.md":
        for field in ("Deck version", "Anki client and version", "Operating system", "affected words", "screenshots", "logs"):
            assert field in body
        assert "Remove personal information" in body
    assert not (directory / "config.yml").exists()
