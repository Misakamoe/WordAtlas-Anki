import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .builder import build, read_json_policy
from .constants import ROOT
from .data import load_dataset, load_identities
from .manifest import load_manifest
from .upstream import checkout_metadata, hydrate_media, input_fingerprint, local_inputs
from .util import BuildError, file_digest, read_json, require_sha, run, safe_file, write_json
from .verify import read_checksums, verify_dist


def gh(*args, payload=None, retry=True, timeout=300):
    executable = os.environ.get("GH_EXECUTABLE", "gh")
    for attempt in range(3 if retry else 1):
        try:
            return run([executable, *args], input=json.dumps(payload, ensure_ascii=False) if payload is not None else None, timeout=timeout)
        except BuildError:
            if not retry or attempt == 2:
                raise
            time.sleep(2 ** attempt)


def api(endpoint, *, method=None, payload=None):
    args = ["api", endpoint]
    if method:
        args += ["--method", method, "--input", "-"]
    return json.loads(gh(*args, payload=payload, retry=method is None))


def check_repo(repo):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise BuildError("Invalid GitHub owner/repository")
    metadata = api(f"repos/{repo}")
    if metadata.get("full_name", "").casefold() != repo.casefold() or metadata.get("private") is not False or metadata.get("archived"):
        raise BuildError("Expected the existing, public, active repository")
    return metadata


def current_builder(repo, metadata):
    sha = require_sha(run(["git", "rev-parse", "HEAD"], cwd=ROOT))
    branch = quote(metadata["default_branch"], safe="")
    remote = api(f"repos/{repo}/branches/{branch}")["commit"]["sha"]
    if remote != sha:
        raise BuildError("Only the current default-branch builder may publish; rerun from the latest default branch")
    return sha


def releases(repo):
    pages = json.loads(gh("api", f"repos/{repo}/releases?per_page=100", "--paginate", "--slurp"))
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise BuildError("Invalid GitHub releases response")
    return [release for page in pages for release in page]


def latest_published(items):
    published = [item for item in items if not item["draft"]]
    return max(published, key=lambda item: (item["published_at"], item["id"])) if published else None


def baseline(repo, release, destination):
    if release is None:
        return None
    names = [asset["name"] for asset in release["assets"]]
    if names.count("manifest.json") != 1 or names.count("SHA256SUMS") != 1:
        raise BuildError("The latest published release lacks a valid baseline; refusing to treat it as a first release")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    gh("release", "download", release["tag_name"], "--repo", repo, "--pattern", "manifest.json", "--pattern", "SHA256SUMS", "--dir", str(destination), "--clobber")
    checksums = read_checksums(destination)
    if checksums.get("manifest.json") != file_digest(safe_file(destination, "manifest.json")):
        raise BuildError("Published baseline checksum mismatch")
    manifest = load_manifest(destination / "manifest.json", require_full=True)
    if release["tag_name"] != "wa-" + manifest["input_fingerprint"]:
        raise BuildError("Published baseline tag does not identify its inputs")
    return manifest


def write_outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in values.items():
                if not re.fullmatch(r"[a-z_]+", key) or "\n" in str(value) or "\r" in str(value):
                    raise BuildError("Unsafe workflow output")
                stream.write(f"{key}={value}\n")


def check_release(repo, upstream, state_path):
    metadata = check_repo(repo)
    builder_sha = current_builder(repo, metadata)
    policy = read_json_policy(ROOT)
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    latest = latest_published(releases(repo))
    previous_path = state_path.parent / "baseline/manifest.json"
    previous = baseline(repo, latest, previous_path.parent)
    upstream_sha = checkout_metadata(upstream)
    dataset = load_dataset(upstream)
    identities, deletions = load_identities(ROOT / "config/identities.json", dataset.entries)
    if previous:
        removed = set(previous["notes"]) - set(identities.values()) - deletions
        if removed:
            raise BuildError(f"Unconfirmed deletion/rename blocks automatic release: {sorted(removed)[:20]}")
    inputs = input_fingerprint(upstream, upstream_sha, dataset)
    changed = previous is None or inputs["fingerprint"] != previous["input_fingerprint"]
    state = {"schema_version": 1, "repo": repo, "builder_sha": builder_sha, "upstream_sha": upstream_sha,
             "upstream": str(Path(upstream).resolve()), "input_fingerprint": inputs["fingerprint"],
             "needs_update": changed, "channel": policy["channel"], "previous_tag": latest["tag_name"] if latest else None,
             "previous_manifest": str(previous_path.resolve()) if previous else None,
             "previous_manifest_sha256": previous["manifest_sha256"] if previous else None}
    write_json(state_path, state)
    write_outputs({"needs_update": str(changed).lower(), "builder_sha": builder_sha, "upstream_sha": upstream_sha, "fingerprint": inputs["fingerprint"]})
    print(json.dumps({"needs_update": changed, "upstream_sha": upstream_sha, "input_fingerprint": inputs["fingerprint"], "previous_tag": state["previous_tag"]}, indent=2))
    return state


def build_release(state_path, out):
    state = read_json(state_path)
    if state.get("needs_update") is not True:
        raise BuildError("No changed release is planned")
    if require_sha(run(["git", "rev-parse", "HEAD"], cwd=ROOT)) != state["builder_sha"]:
        raise BuildError("Builder commit changed after preflight")
    dataset = load_dataset(state["upstream"])
    if input_fingerprint(state["upstream"], state["upstream_sha"], dataset)["fingerprint"] != state["input_fingerprint"]:
        raise BuildError("Inputs changed after preflight")
    if state["previous_manifest"]:
        if load_manifest(state["previous_manifest"], require_full=True)["manifest_sha256"] != state["previous_manifest_sha256"]:
            raise BuildError("Baseline changed after preflight")
    hydrate_media(state["upstream"], state["upstream_sha"], dataset.entries)
    manifest = build(state["upstream"], state["upstream_sha"], out, previous_manifest=state["previous_manifest"])
    if manifest["input_fingerprint"] != state["input_fingerprint"]:
        raise BuildError("Built artifact does not match the preflight inputs")
    return manifest


def verify_remote_assets(repo, release, directory):
    directory = Path(directory)
    expected = {path.name: {"bytes": path.stat().st_size, "sha256": file_digest(path)} for path in directory.iterdir() if path.is_file()}
    assets = release["assets"]
    if len(assets) != len(expected) or {asset["name"] for asset in assets} != set(expected):
        raise BuildError("Uploaded attachment set is incomplete or unexpected")
    for asset in assets:
        record = expected[asset["name"]]
        if asset.get("state") != "uploaded" or asset["size"] != record["bytes"]:
            raise BuildError(f"Uploaded asset length/state mismatch: {asset['name']}")
        if asset.get("digest"):
            if asset["digest"] != "sha256:" + record["sha256"]:
                raise BuildError(f"Uploaded asset digest mismatch: {asset['name']}")
        else:
            with tempfile.TemporaryDirectory(prefix="wa-asset-check-") as temp:
                gh("release", "download", release["tag_name"], "--repo", repo, "--pattern", asset["name"], "--dir", temp)
                if file_digest(safe_file(temp, asset["name"])) != record["sha256"]:
                    raise BuildError("Downloaded asset checksum mismatch")


def publish_release(repo, out, state_path):
    state = read_json(state_path)
    metadata = check_repo(repo)
    builder_sha = current_builder(repo, metadata)
    policy = read_json_policy(ROOT)
    manifest = verify_dist(out, release=True)
    if manifest["input_files"] != local_inputs(ROOT):
        raise BuildError("The candidate was built with different code or resources")
    if run(["git", "status", "--porcelain", "--untracked-files=all", "--", "src", "templates", "config", "assets", "licenses", "pyproject.toml", "requirements.lock", "LICENSE"], cwd=ROOT):
        raise BuildError("Publishing uncommitted build inputs is not allowed")
    if state.get("repo") != repo or state.get("needs_update") is not True:
        raise BuildError("Missing or mismatched release preflight")
    for key in ("builder_sha", "upstream_sha", "input_fingerprint", "channel"):
        if state.get(key) != manifest.get(key):
            raise BuildError(f"Publication does not match preflight: {key}")
    if builder_sha != state["builder_sha"] or policy["channel"] != manifest["channel"] or policy["client_evidence"] != manifest["client_evidence"]:
        raise BuildError("Builder or verification policy changed")
    tag = "wa-" + manifest["input_fingerprint"]
    items = releases(repo)
    matches = [release for release in items if release["tag_name"] == tag]
    if len(matches) > 1:
        raise BuildError("Multiple releases claim the same input fingerprint")
    release = matches[0] if matches else None
    if release and not release["draft"]:
        # Never overwrite public releases. A same-input retry is a read-only check.
        verify_remote_assets(repo, release, out)
        print(release["html_url"])
        return release["html_url"]
    latest = latest_published(items)
    if (latest["tag_name"] if latest else None) != state["previous_tag"]:
        raise BuildError("Another version was published after preflight; rebuild against its baseline")
    prerelease = policy["channel"] == "prerelease"
    title_date = datetime.fromtimestamp(manifest["created_at"], timezone.utc).strftime("%Y-%m-%d")
    payload = {"tag_name": tag, "target_commitish": builder_sha, "name": f'Word Atlas {title_date}' + (" 预发布" if prerelease else ""),
               "body": safe_file(out, "release-notes.md").read_text(encoding="utf-8"), "draft": True,
               "prerelease": prerelease, "make_latest": "false"}
    if release is None:
        release = api(f"repos/{repo}/releases", method="POST", payload=payload)
    if not release["draft"]:
        raise BuildError("Uploads are only permitted to a draft")
    out = Path(out)
    for path in sorted(out.iterdir()):
        for attempt in range(3):
            # A failed upload may have succeeded remotely. Guard every retry too.
            if not api(f"repos/{repo}/releases/{release['id']}")["draft"]:
                raise BuildError("Draft was made public during upload; refusing to overwrite it")
            try:
                gh("release", "upload", tag, str(path), "--repo", repo, "--clobber", retry=False, timeout=900)
                break
            except BuildError:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    uploaded = api(f"repos/{repo}/releases/{release['id']}")
    if not uploaded["draft"]:
        raise BuildError("Draft changed while validating uploads")
    verify_remote_assets(repo, uploaded, out)
    # Also re-check trusted source HEAD before advancing latest.
    current_builder(repo, metadata)
    current = api(f"repos/{repo}/releases/{release['id']}")
    if not current["draft"]:
        verify_remote_assets(repo, current, out)
        print(current["html_url"])
        return current["html_url"]
    payload.update(draft=False, make_latest="false" if prerelease else "true")
    result = api(f"repos/{repo}/releases/{release['id']}", method="PATCH",
                 payload=payload)
    if result["draft"] or result["prerelease"] != prerelease:
        raise BuildError("GitHub did not finalize the requested release state")
    verify_remote_assets(repo, result, out)
    print(result["html_url"])
    return result["html_url"]
