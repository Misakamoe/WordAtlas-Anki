import re
from collections import Counter

from .constants import DECK_IDS, FIELD_IDS, FIELDS, LEVELS, MODEL_ID, ROOT_DECK_ID, SCHEMA_VERSION, TEMPLATE_ID
from .data import valid_id
from .util import BuildError, digest, read_json, require_sha

IDENTITY = {"model": MODEL_ID, "root_deck": ROOT_DECK_ID, "decks": DECK_IDS,
            "fields": dict(zip(FIELDS, FIELD_IDS)), "template": TEMPLATE_ID, "template_ordinal": 0}


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def seal(manifest):
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = digest(manifest)
    return manifest


def load_manifest(path, *, require_full=False):
    import genanki
    value = read_json(path)
    if not isinstance(value, dict):
        raise BuildError("Invalid previous manifest")
    expected = value.get("manifest_sha256")
    if not sha256(expected) or digest({k: v for k, v in value.items() if k != "manifest_sha256"}) != expected:
        raise BuildError("Manifest integrity check failed")
    if value.get("schema_version") != SCHEMA_VERSION or value.get("identity") != IDENTITY:
        raise BuildError("Incompatible manifest schema or Anki identities")
    if type(value.get("full")) is not bool or type(value.get("fixture")) is not bool or (require_full and (not value["full"] or value["fixture"])):
        raise BuildError("An incomplete/fixture package cannot be a release baseline")
    require_sha(value.get("upstream_sha"))
    if value.get("builder_sha") == "uncommitted":
        if require_full:
            raise BuildError("A published release requires a committed builder SHA")
    else:
        require_sha(value.get("builder_sha"))
    if not sha256(value.get("input_fingerprint")) or not sha256(value.get("model", {}).get("sha256")):
        raise BuildError("Invalid manifest fingerprints")
    notes = value.get("notes")
    if not isinstance(notes, dict) or not notes or type(value.get("note_count")) is not int or value.get("note_count") != len(notes):
        raise BuildError("Invalid manifest note count")
    if type(value.get("upstream_count")) is not int or value["upstream_count"] < len(notes) or (value["full"] and value["upstream_count"] != len(notes)):
        raise BuildError("Manifest completeness does not match its source count")
    counts = Counter()
    for ident, note in notes.items():
        valid_id(ident)
        if not isinstance(note, dict) or note.get("guid") != genanki.guid_for("word-atlas-anki", ident):
            raise BuildError(f"Invalid manifest GUID: {ident}")
        valid_id(note.get("source_id"))
        valid_id(note.get("topic"))
        if not isinstance(note.get("word"), str) or not note["word"] or type(note.get("order")) is not int or note["order"] < 0:
            raise BuildError(f"Invalid manifest word/order: {ident}")
        if type(note.get("mod")) is not int or note["mod"] <= 0 or not sha256(note.get("content_sha256")):
            raise BuildError(f"Invalid manifest note timestamp/digest: {ident}")
        if set(note.get("fields", {})) != set(FIELDS) or not all(sha256(h) for h in note["fields"].values()):
            raise BuildError(f"Invalid manifest field contract: {ident}")
        if note.get("cefr") not in LEVELS or not isinstance(note.get("media"), dict):
            raise BuildError(f"Invalid manifest classification/media: {ident}")
        counts[note["cefr"]] += 1
    if value.get("level_counts") != {level: counts[level] for level in LEVELS}:
        raise BuildError("Manifest level counts disagree")
    if type(value["model"].get("mod")) is not int or value["model"]["mod"] <= 0:
        raise BuildError("Invalid note-type timestamp")
    parts = value.get("packages")
    if not isinstance(parts, list) or not parts:
        raise BuildError("Missing package manifest")
    members = []
    for part in parts:
        if not isinstance(part, dict) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.apkg", part.get("name", "")):
            raise BuildError("Unsafe package filename")
        if not sha256(part.get("sha256")) or type(part.get("bytes")) is not int or not 0 < part["bytes"] < 2 * 1024**3:
            raise BuildError("Invalid package checksum or size")
        members.extend(part.get("notes", []))
    if len(members) != len(set(members)) or set(members) != set(notes):
        raise BuildError("Package partition has missing or duplicate notes")
    media = value.get("media")
    if not isinstance(media, dict) or len(media) != value.get("media_count"):
        raise BuildError("Invalid manifest media count")
    for name, record in media.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not sha256(record.get("sha256")) or type(record.get("bytes")) is not int or record["bytes"] <= 0:
            raise BuildError("Invalid manifest media record")
    for note in notes.values():
        for kind, name in note["media"].items():
            if kind not in ("img", "uk", "us", "ex", "ex2") or name not in media:
                raise BuildError("Note references unknown manifest media")
    return value


def changes_between(previous, current):
    before = previous["notes"] if previous else {}
    after = current["notes"]
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys()) if current["full"] else []
    changed = []
    classifications = []
    fields_count = Counter()
    media_count = Counter()
    for ident in sorted(before.keys() & after.keys()):
        old, new = before[ident], after[ident]
        fields = [field for field in FIELDS if old["fields"][field] != new["fields"][field]]
        media = [kind for kind in ("img", "uk", "us", "ex", "ex2") if old["media"].get(kind) != new["media"].get(kind)]
        if old["content_sha256"] != new["content_sha256"] or old["source_id"] != new["source_id"]:
            changed.append({"id": ident, "word": new["word"], "fields": fields, "media": media,
                            "previous_source_id": old["source_id"], "source_id": new["source_id"]})
            fields_count.update(fields)
            media_count.update(media)
        if (old["cefr"], old["topic"]) != (new["cefr"], new["topic"]):
            classifications.append({"id": ident, "word": new["word"], "before": [old["cefr"], old["topic"]], "after": [new["cefr"], new["topic"]]})
    return {"schema_version": SCHEMA_VERSION, "initial": previous is None,
            "previous_upstream_sha": previous["upstream_sha"] if previous else None,
            "upstream_sha": current["upstream_sha"],
            "added": [{"id": ident, "word": after[ident]["word"]} for ident in added],
            "modified": changed, "confirmed_deleted": [{"id": ident, "word": before[ident]["word"]} for ident in removed],
            "field_changes": dict(sorted(fields_count.items())), "media_changes": dict(sorted(media_count.items())),
            "classification_changes": classifications,
            "template_changed": previous is None or previous["model"]["sha256"] != current["model"]["sha256"],
            "builder_changed": previous is None or previous["input_components"]["local"] != current["input_components"]["local"]}
