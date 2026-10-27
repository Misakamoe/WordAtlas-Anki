import hashlib
import json
import re
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from .constants import FIELDS, MODEL_ID
from .manifest import IDENTITY, load_manifest
from .util import BuildError, file_digest, safe_file


def read_checksums(directory):
    result = {}
    for line in safe_file(directory, "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in result:
            raise BuildError("Malformed or duplicate checksum entry")
        result[match[2]] = match[1]
    if not result:
        raise BuildError("Empty checksums")
    return result


def verify_package(path, part, manifest):
    if path.stat().st_size != part["bytes"] or file_digest(path) != part["sha256"]:
        raise BuildError(f"Package integrity mismatch: {path.name}")
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        if len(set(members)) != len(members) or "collection.anki2" not in members or "media" not in members:
            raise BuildError("Malformed APKG archive")
        if archive.getinfo("media").file_size > 16 * 1024**2 or archive.getinfo("collection.anki2").file_size > 256 * 1024**2:
            raise BuildError("Unexpectedly large package metadata")
        mapping = json.loads(archive.read("media"))
        if not isinstance(mapping, dict) or set(mapping) != {str(i) for i in range(len(mapping))}:
            raise BuildError("Invalid APKG media index")
        if len(mapping.values()) != len(set(mapping.values())) or set(members) != {"collection.anki2", "media", *mapping.keys()}:
            raise BuildError("Duplicate or unexpected APKG members")
        expected_media = {name for name in manifest["media"] if name.startswith("_wa_")}
        for ident in part["notes"]:
            expected_media.update(manifest["notes"][ident]["media"].values())
        if set(mapping.values()) != expected_media or len(mapping) != part["media_count"]:
            raise BuildError("Package media coverage disagrees with manifest")
        for member, name in mapping.items():
            record = manifest["media"][name]
            if archive.getinfo(member).file_size != record["bytes"]:
                raise BuildError(f"Media byte length mismatch: {name}")
            with archive.open(member) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != record["sha256"]:
                raise BuildError(f"Media checksum mismatch: {name}")
        with tempfile.TemporaryDirectory(prefix="wa-verify-") as temp:
            database = Path(temp) / "collection.anki2"
            database.write_bytes(archive.read("collection.anki2"))
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA trusted_schema=OFF")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BuildError("Invalid collection database")
                models = json.loads(connection.execute("SELECT models FROM col").fetchone()[0])
                if set(models) != {str(MODEL_ID)}:
                    raise BuildError("Package contains an unexpected note type")
                model = models[str(MODEL_ID)]
                if [f["name"] for f in model["flds"]] != list(FIELDS) or {f["name"]: f.get("id") for f in model["flds"]} != IDENTITY["fields"]:
                    raise BuildError("Field identity contract changed")
                if len(model["tmpls"]) != 1 or model["tmpls"][0].get("id") != IDENTITY["template"] or model["tmpls"][0]["ord"] != 0:
                    raise BuildError("Template identity contract changed")
                if model["mod"] != manifest["model"]["mod"]:
                    raise BuildError("Note type modification time disagrees")
                rows = connection.execute("SELECT id,guid,mid,mod,flds FROM notes").fetchall()
                guids = {manifest["notes"][ident]["guid"]: ident for ident in part["notes"]}
                if len(rows) != len(guids) or {row[1] for row in rows} != set(guids):
                    raise BuildError("Package has duplicate/missing note GUIDs")
                from .util import digest
                for nid, guid, mid, modified, fields in rows:
                    note = manifest["notes"][guids[guid]]
                    if mid != MODEL_ID or modified != note["mod"]:
                        raise BuildError("Note identity/timestamp mismatch")
                    values = fields.split("\x1f")
                    if len(values) != len(FIELDS) or {name: digest(value) for name, value in zip(FIELDS, values)} != note["fields"]:
                        raise BuildError("Package field content disagrees with manifest")
                cards = connection.execute("SELECT nid,ord,type,queue,reps,lapses,due FROM cards").fetchall()
                if len(cards) != len(rows) or {card[0] for card in cards} != {row[0] for row in rows} or any(any(card[1:6]) for card in cards):
                    raise BuildError("Expected exactly one fresh forward card per note, without study history")
                by_note = {row[0]: manifest["notes"][guids[row[1]]] for row in rows}
                if any(card[6] != by_note[card[0]]["order"] + 1 for card in cards):
                    raise BuildError("New-card positions do not follow the upstream learning order")
                if connection.execute("SELECT count(*) FROM revlog").fetchone()[0] != 0:
                    raise BuildError("Author study history must not be included")
    return expected_media


def verify_dist(directory, *, release=False):
    directory = Path(directory)
    checksums = read_checksums(directory)
    manifest = load_manifest(directory / "manifest.json", require_full=release)
    expected = {part["name"] for part in manifest["packages"]} | {"manifest.json", "changes.json", "release-notes.md"}
    if set(checksums) != expected or {p.name for p in directory.iterdir()} != expected | {"SHA256SUMS"}:
        raise BuildError("Output/attachment set is incomplete or contains unexpected files")
    for name, checksum in checksums.items():
        if file_digest(safe_file(directory, name)) != checksum:
            raise BuildError(f"Checksum mismatch: {name}")
    seen_media = set()
    for part in manifest["packages"]:
        seen_media.update(verify_package(directory / part["name"], part, manifest))
    if seen_media != set(manifest["media"]):
        raise BuildError("Partitioned packages omit media")
    return manifest
