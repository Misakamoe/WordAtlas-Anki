import hashlib
import itertools
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections import Counter
from contextlib import closing
from pathlib import Path

import genanki

from .constants import DECK_DESCRIPTION, DECK_IDS, FIELD_IDS, FIELDS, LEVELS, MAX_PACKAGE_BYTES, MODEL_ID, ROOT, ROOT_DECK_ID, SCHEMA_VERSION, TEMPLATE_ID
from .data import Dataset, load_dataset, load_identities, media_paths
from .manifest import IDENTITY, changes_between, load_manifest, seal
from .media import MediaStore, decode_media
from .render import SOURCE, render_fields
from .upstream import check_licenses, input_fingerprint, local_inputs, verify_checkout
from .util import BuildError, digest, file_digest, require_sha, run, safe_file, write_json


class StableNote(genanki.Note):
    def __init__(self, *, modified, **kwargs):
        super().__init__(**kwargs)
        self.modified = modified

    def write_to_db(self, cursor, timestamp, deck_id, id_gen):
        super().write_to_db(cursor, self.modified, deck_id, id_gen)


class StableModel(genanki.Model):
    def __init__(self, *, modified, **kwargs):
        super().__init__(**kwargs)
        self.modified = modified

    def to_json(self, timestamp, deck_id):
        return super().to_json(self.modified, ROOT_DECK_ID)


class CleanDeck(genanki.Deck):
    def to_json(self):
        value = super().to_json()
        for key in ("lrnToday", "newToday", "revToday", "timeToday"):
            value[key] = [0, 0]
        return value


def templates(project, static_names):
    script = safe_file(project, "templates/card.js").read_text(encoding="utf-8")
    if "</script" in script.lower():
        raise BuildError("Inline card script contains an HTML closing delimiter")
    result = {}
    for name in ("front.html", "back.html", "card.css"):
        value = safe_file(project, "templates/" + name).read_text(encoding="utf-8")
        if name.endswith(".html"):
            if value.count("<!-- WA_SCRIPT -->") != 1 or "{{FrontSide}}" in value:
                raise BuildError("Invalid template script/FrontSide contract")
            value = value.replace("<!-- WA_SCRIPT -->", "<script>\n" + script + "\n</script>")
        for before, after in static_names.items():
            value = value.replace(before, after)
        result[name] = value
    return result


def make_model(template, modified):
    return StableModel(modified=modified, model_id=MODEL_ID, name="Word Atlas - Offline v1",
                       fields=[{"name": name, "id": ident} for name, ident in zip(FIELDS, FIELD_IDS)],
                       templates=[{"name": "Recall", "id": TEMPLATE_ID, "qfmt": template["front.html"], "afmt": template["back.html"]}],
                       css=template["card.css"], sort_field_index=FIELDS.index("Word"))


def _write_package(path, rows, model, store, timestamp):
    decks = {level: CleanDeck(DECK_IDS[level], "Word Atlas::" + level, DECK_DESCRIPTION) for level in LEVELS}
    media = {name for name in store.files if name.startswith("_wa_")}
    for row in rows:
        record = row["record"]
        media.update(record["media"].values())
        note = StableNote(model=model, fields=[row["fields"][name] for name in FIELDS], guid=record["guid"],
                          tags=row["tags"], due=record["order"] + 1, modified=record["mod"])
        decks[record["cefr"]].add_note(note)
    root = CleanDeck(ROOT_DECK_ID, "Word Atlas", DECK_DESCRIPTION)
    package = genanki.Package([root, *decks.values()])
    with tempfile.TemporaryDirectory(prefix="package-", dir=path.parent) as temp:
        database = Path(temp) / "collection.anki2"
        with closing(sqlite3.connect(database)) as connection:
            package.write_to_db(connection.cursor(), timestamp, itertools.count(timestamp * 1000))
            connection.commit()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            def add(name, source=None, content=None):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                if source is not None:
                    with archive.open(info, "w", force_zip64=True) as target, source.open("rb") as stream:
                        shutil.copyfileobj(stream, target, 1024 * 1024)
                else:
                    archive.writestr(info, content)
            add("collection.anki2", database)
            names = sorted(media)
            add("media", content=json.dumps(dict(enumerate(names)), sort_keys=True).encode("utf-8"))
            for i, name in enumerate(names):
                add(str(i), store.files[name])
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": file_digest(path),
            "notes": sorted(row["id"] for row in rows), "media_count": len(media)}


def write_packages(out, rows, model, store, timestamp, fingerprint, *, sample=False, limit=MAX_PACKAGE_BYTES):
    if limit > MAX_PACKAGE_BYTES or limit <= 0:
        raise BuildError("Invalid package size limit")
    prefix = "Word-Atlas-" + ("INCOMPLETE-SAMPLE-" if sample else "")
    suffix = "-" + fingerprint[:16] + ".apkg"

    def write_part(label, group):
        path = out / (prefix + label + suffix)
        result = _write_package(path, group, model, store, timestamp)
        if result["bytes"] < limit:
            return [result]
        path.unlink()
        if len(group) <= 1:
            raise BuildError("A single note and its media exceed the safe GitHub attachment limit")
        ordered = sorted(group, key=lambda row: row["id"])
        middle = len(ordered) // 2
        return write_part(label + "-1", ordered[:middle]) + write_part(label + "-2", ordered[middle:])

    full_path = out / (prefix + "Full" + suffix)
    result = _write_package(full_path, rows, model, store, timestamp)
    if result["bytes"] < limit:
        return [result]
    full_path.unlink()
    parts = []
    for level in LEVELS:
        group = [row for row in rows if row["record"]["cefr"] == level]
        if group:
            parts.extend(write_part(level, group))
    return parts


def select_sample(entries, count):
    if count <= 0 or count >= len(entries):
        raise BuildError("Sample count must be positive and smaller than the full dataset")
    groups = [[e for e in entries if e["cefr"] == level] for level in LEVELS]
    selection = set()
    for group in itertools.zip_longest(*groups):
        for entry in group:
            if entry and len(selection) < count:
                selection.add(entry["id"])
        if len(selection) == count:
            break
    return [entry for entry in entries if entry["id"] in selection]


def build(upstream, upstream_sha, out, *, previous_manifest=None, sample=None, project=ROOT, cache=None,
          now=None, verify_upstream=True, package_limit=MAX_PACKAGE_BYTES):
    require_sha(upstream_sha)
    upstream, out, project = Path(upstream), Path(out), Path(project)
    cache = Path(cache) if cache else project / ".cache"
    now = int(time.time()) if now is None else int(now)
    if now <= 0:
        raise BuildError("Invalid build timestamp")
    if out.exists() and any(out.iterdir()):
        raise BuildError("Output directory must be empty; previous builds are never silently overwritten")
    dataset = load_dataset(upstream)
    previous = load_manifest(previous_manifest) if previous_manifest else None
    identities, deletions = load_identities(project / "config/identities.json", dataset.entries)
    missing = (set(previous["notes"]) - set(identities.values())) if previous else set()
    if sample is None and missing - deletions:
        raise BuildError(f"Unconfirmed deletion/rename blocks publication: {sorted(missing - deletions)[:20]}")
    if verify_upstream:
        verify_checkout(upstream, upstream_sha)
        inputs = input_fingerprint(upstream, upstream_sha, dataset, project)
    else:
        local = local_inputs(project)
        parts = {"dataset": dataset.fingerprint, "local": digest(local), "licenses": digest(check_licenses(upstream, project)),
                 "media": digest({p: file_digest(safe_file(upstream, p)) for p in media_paths(dataset.entries).values()})}
        inputs = {"fingerprint": digest(parts), "components": parts, "local_files": local, "license_files": check_licenses(upstream, project), "unreferenced_media": []}
    entries = select_sample(dataset.entries, sample) if sample is not None else dataset.entries
    fingerprint = digest({"input": inputs["fingerprint"], "sample": [e["id"] for e in entries]}) if sample is not None else inputs["fingerprint"]
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        builder_sha = run(["git", "rev-parse", "HEAD"], cwd=project)
    except BuildError:
        builder_sha = "uncommitted"
    with tempfile.TemporaryDirectory(prefix="wa-media-", dir=cache) as temp:
        store = MediaStore(temp)
        print(f"Staging media for {len(entries)} notes", flush=True)
        media = store.collect(upstream, entries)
        store.add_static(project)
        decoding = decode_media(store, cache / "media-validation.json")
        template = templates(project, store.static_names)
        # Anki drops unreferenced text media on export, but preserves note-type CSS.
        legal = SOURCE + "\n\n" + "\n\n".join(store.files[name].read_text(encoding="utf-8") for name in sorted(store.files) if store.records[name]["type"] == "license")
        if "*/" in legal or "</style" in legal.lower():
            raise BuildError("License notices contain an unsafe CSS/HTML comment delimiter")
        template["card.css"] += "\n/* Bundled attribution and license notices (not displayed).\n" + legal + "\n*/\n"
        model_hash = digest({"templates": template, "identity": IDENTITY})
        old_model = previous["model"] if previous else None
        model_mod = old_model["mod"] if old_model and old_model["sha256"] == model_hash else max(now, (old_model["mod"] + 1) if old_model else now)
        model = make_model(template, model_mod)
        rows = []
        order = {entry["id"]: i for i, entry in enumerate(dataset.entries)}
        for entry in entries:
            ident = identities[entry["id"]]
            fields = render_fields(entry, ident, dataset.topics[entry["topic"]], media[entry["id"]])
            tags = ["word_atlas", "word_atlas::cefr::" + entry["cefr"], "word_atlas::topic::" + entry["topic"]]
            content_hash = digest({"fields": fields, "tags": tags})
            old = previous["notes"].get(ident) if previous else None
            modified = old["mod"] if old and old["content_sha256"] == content_hash else max(now, old["mod"] + 1 if old else now)
            record = {"source_id": entry["id"], "guid": genanki.guid_for("word-atlas-anki", ident), "word": entry["w"],
                      "cefr": entry["cefr"], "topic": entry["topic"], "order": order[entry["id"]], "mod": modified,
                      "content_sha256": content_hash, "fields": {name: digest(value) for name, value in fields.items()},
                      "media": {kind: value["name"] for kind, value in media[entry["id"]].items()}}
            rows.append({"id": ident, "record": record, "fields": fields, "tags": tags})
        package_time = max([model_mod, *(row["record"]["mod"] for row in rows)])
        parts = write_packages(out, rows, model, store, package_time, fingerprint, sample=sample is not None, limit=package_limit)
        counts = Counter(entry["cefr"] for entry in entries)
        manifest = {"schema_version": SCHEMA_VERSION, "identity": IDENTITY, "full": sample is None,
                    "fixture": not verify_upstream, "upstream_sha": upstream_sha, "builder_sha": builder_sha,
                    "input_fingerprint": fingerprint, "input_components": inputs["components"], "input_files": inputs["local_files"],
                    "upstream_licenses": inputs["license_files"], "created_at": now,
                    "note_count": len(rows), "upstream_count": len(dataset.entries), "level_counts": {level: counts[level] for level in LEVELS},
                    "coverage": Dataset(entries, dataset.topics, dataset.index).coverage,
                    "model": {"sha256": model_hash, "mod": model_mod}, "notes": {row["id"]: row["record"] for row in rows},
                    "media": store.records, "media_count": len(store.records), "media_bytes": sum(r["bytes"] for r in store.records.values()),
                    "media_validation": decoding, "unreferenced_media": inputs["unreferenced_media"], "packages": parts,
                    "client_evidence": read_json_policy(project)["client_evidence"], "channel": read_json_policy(project)["channel"]}
        changes = changes_between(previous, manifest)
        write_json(out / "changes.json", changes)
        from .release_notes import release_notes
        (out / "release-notes.md").write_text(release_notes(manifest, changes), encoding="utf-8", newline="\n")
        seal(manifest)
        write_json(out / "manifest.json", manifest)
        checksummed = [part["name"] for part in parts] + ["manifest.json", "changes.json", "release-notes.md"]
        (out / "SHA256SUMS").write_text("".join(f"{file_digest(out / name)}  {name}\n" for name in sorted(checksummed)), encoding="utf-8", newline="\n")
    from .verify import verify_dist
    verify_dist(out)
    print(f"Built {len(rows)} notes, {manifest['media_count']} media, {sum(p['bytes'] for p in parts)} package bytes", flush=True)
    return manifest


def read_json_policy(project):
    from .util import read_json
    policy = read_json(safe_file(project, "config/release-policy.json"))
    if policy.get("channel") not in ("prerelease", "stable") or set(policy.get("client_evidence", {})) != {"Anki", "AnkiDroid", "AnkiMobile"}:
        raise BuildError("Invalid release policy")
    if policy["channel"] == "stable":
        for client, evidence in policy["client_evidence"].items():
            if not isinstance(evidence, dict) or not evidence.get("version") or not evidence.get("tested_at") or not evidence.get("record") or not all(evidence.get(k) is True for k in ("offline", "audio", "theme", "upgrade")):
                raise BuildError(f"Stable release needs recorded real-device evidence for {client}")
    return policy
