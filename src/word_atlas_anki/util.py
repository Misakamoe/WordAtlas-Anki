import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath


class BuildError(ValueError):
    """An input or publication invariant was violated."""


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_file(root, relative):
    root = Path(root).absolute()
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise BuildError(f"Links are not allowed as input roots: {root}")
    rel = PurePosixPath(relative)
    if not relative or "\\" in relative or ":" in relative or rel.is_absolute() or any(p in ("", ".", "..") for p in relative.split("/")):
        raise BuildError(f"Unsafe input path: {relative!r}")
    current = root
    for component in rel.parts:
        current = current / component
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise BuildError(f"Links are not allowed: {current}")
    try:
        mode = current.stat().st_mode
    except FileNotFoundError as exc:
        raise BuildError(f"Missing input file: {current}") from exc
    if not stat.S_ISREG(mode) or not current.resolve().is_relative_to(root.resolve()):
        raise BuildError(f"Not an ordinary input file: {current}")
    return current


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BuildError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda x: (_ for _ in ()).throw(BuildError(f"Invalid JSON constant: {x}")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def run(args, *, cwd=None, timeout=180, input=None):
    try:
        result = subprocess.run([str(a) for a in args], cwd=cwd,
                                input=input.encode("utf-8") if isinstance(input, str) else input,
                                capture_output=True, timeout=timeout, check=True)
        return result.stdout.decode("utf-8", errors="replace").rstrip("\r\n")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise BuildError(f"Command failed ({args[0]}): {detail}") from exc


def require_sha(sha):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BuildError("An exact, lowercase, 40-character commit SHA is required")
    return sha
