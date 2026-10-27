import time
from pathlib import Path

from .constants import LICENSE_PATHS, ROOT, UPSTREAM_REPO
from .data import media_paths
from .util import BuildError, digest, file_digest, read_json, require_sha, run, safe_file

BASE_PATHS = ("data/word-atlas.json", "public/atlas/index.json", *LICENSE_PATHS)
MEDIA_DIRS = ("public/atlas/img", *(f"public/atlas/audio/{kind}" for kind in ("uk", "us", "ex", "ex2")))


def git(root, *args, timeout=180, input=None):
    return run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, timeout=timeout, input=input)


def resolve_main():
    result = run(["git", "ls-remote", UPSTREAM_REPO + ".git", "refs/heads/main"])
    rows = result.splitlines()
    if len(rows) != 1:
        raise BuildError("Could not resolve upstream main uniquely")
    return require_sha(rows[0].split()[0])


def checkout_metadata(destination, sha=None):
    sha = require_sha(sha) if sha else resolve_main()
    destination = Path(destination).absolute()
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "-c", "core.autocrlf=false", "clone", "--depth=1", "--filter=blob:none", "--no-checkout",
             "--single-branch", "--branch", "main", UPSTREAM_REPO + ".git", destination], timeout=600)
    if not (destination / ".git").is_dir() or destination.is_symlink():
        raise BuildError("Upstream destination must be a dedicated, ordinary Git checkout")
    if git(destination, "remote", "get-url", "origin").removesuffix(".git") != UPSTREAM_REPO:
        raise BuildError("Refusing to reuse a checkout of another repository")
    # A new --no-checkout clone reports index deletions until its first checkout.
    if git(destination, "ls-files") and git(destination, "status", "--porcelain", "--untracked-files=no", "--", *BASE_PATHS, *MEDIA_DIRS):
        raise BuildError("Upstream cache has local modifications; use a different cache directory")
    git(destination, "fetch", "--depth=1", "--filter=blob:none", "origin", sha, timeout=600)
    git(destination, "sparse-checkout", "set", "--no-cone", *("/" + p for p in BASE_PATHS))
    git(destination, "checkout", "--detach", sha, timeout=600)
    verify_checkout(destination, sha)
    return sha


def verify_checkout(root, sha):
    require_sha(sha)
    if git(root, "rev-parse", "HEAD") != sha:
        raise BuildError("Upstream checkout does not match the requested SHA")
    if git(root, "status", "--porcelain", "--untracked-files=no", "--", *BASE_PATHS, *MEDIA_DIRS):
        raise BuildError("Pinned upstream files were modified")


def media_tree(root, sha):
    require_sha(sha)
    rows = git(root, "ls-tree", "-rz", sha, "--", *MEDIA_DIRS)
    result = {}
    for row in rows.split("\0"):
        if not row:
            continue
        metadata, path = row.split("\t", 1)
        mode, kind, blob = metadata.split()
        if mode not in ("100644", "100755") or kind != "blob":
            raise BuildError(f"Non-regular upstream media: {path}")
        result[path] = blob
    return result


def hydrate_media(root, sha, entries, *, full=True):
    verify_checkout(root, sha)
    tree = media_tree(root, sha)
    referenced = set(media_paths(entries).values())
    missing = referenced - tree.keys()
    if missing:
        raise BuildError(f"Declared media absent from pinned commit: {sorted(missing)[:5]}")
    # Small, resumable Git object batches avoid restarting a multi-GB lazy fetch
    # after a transient TLS disconnect. Only already-validated tree blobs are wanted.
    available = set(git(root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)", timeout=300).splitlines())
    wanted = sorted({tree[path] for path in (tree if full else referenced)} - available)
    print(f"Fetching {len(wanted)} missing media blobs in bounded batches", flush=True)
    for start in range(0, len(wanted), 128):
        batch = wanted[start:start + 128]
        for attempt in range(3):
            try:
                git(root, "-c", "fetch.negotiationAlgorithm=noop", "-c", "http.version=HTTP/1.1",
                    "fetch", "origin", "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no",
                    "--filter=blob:none", "--stdin", input="\n".join(batch) + "\n", timeout=600)
                break
            except BuildError:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if start % 1280 == 0 or start + 128 >= len(wanted):
            print(f"Fetched {min(start + 128, len(wanted))}/{len(wanted)} media blobs", flush=True)
    patterns = ["/" + p for p in BASE_PATHS]
    patterns += (["/" + p + "/" for p in MEDIA_DIRS] if full else ["/" + p for p in sorted(referenced)])
    git(root, "sparse-checkout", "set", "--no-cone", "--stdin", input="\n".join(patterns) + "\n", timeout=3600)
    verify_checkout(root, sha)


def license_hashes(root):
    # Git checkouts can differ in line endings, not in license text.
    import hashlib
    return {path: hashlib.sha256(safe_file(root, path).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for path in LICENSE_PATHS}


def check_licenses(root, project=ROOT):
    reviewed = read_json(safe_file(project, "config/upstream-licenses.json"))
    current = license_hashes(root)
    if reviewed.get("files") != current:
        raise BuildError("Upstream licenses changed or have not been reviewed; unattended publication is blocked")
    return current


def local_inputs(project=ROOT):
    project = Path(project)
    names = ["pyproject.toml", "requirements.lock", "LICENSE"]
    for directory in ("src/word_atlas_anki", "templates", "assets", "licenses", "config"):
        for path in (project / directory).rglob("*"):
            if "__pycache__" not in path.parts and (path.is_file() or path.is_symlink()):
                names.append(path.relative_to(project).as_posix())
    result = {}
    for name in sorted(names):
        path = safe_file(project, name)
        if path.suffix in (".py", ".html", ".css", ".js", ".json", ".txt", ".toml", ".lock"):
            result[name] = digest(path.read_text(encoding="utf-8"))
        else:
            result[name] = file_digest(path)
    return result


def input_fingerprint(root, sha, dataset, project=ROOT):
    licenses = check_licenses(root, project)
    tree = media_tree(root, sha)
    selected = {}
    for path in media_paths(dataset.entries).values():
        if path not in tree:
            raise BuildError(f"Declared media absent from pinned commit: {path}")
        selected[path] = tree[path]
    local = local_inputs(project)
    parts = {"dataset": dataset.fingerprint, "media": digest(selected), "local": digest(local), "licenses": digest(licenses)}
    return {"fingerprint": digest(parts), "components": parts, "local_files": local,
            "license_files": licenses, "unreferenced_media": sorted(tree.keys() - selected.keys())}
