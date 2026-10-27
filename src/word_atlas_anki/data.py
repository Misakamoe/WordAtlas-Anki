import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .constants import LEVELS, MEDIA_BITS
from .util import BuildError, digest, read_json, safe_file

ID_RE = re.compile(r"[a-z0-9-]{1,64}\Z")
TEXT_KEYS = {"id", "w", "uk", "us", "senses", "ex", "col", "fam", "syn", "ant", "tip", "cefr", "topic", "note"}
INDEX_KEYS = {"id", "version", "name", "license", "count", "examples", "illustrated", "voiced", "chunk", "topics", "cefr", "fields", "words"}


def valid_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise BuildError(f"Invalid Atlas ID: {value!r}")
    return value


def text(value, where, *, required=False):
    if not isinstance(value, str) or len(value) > 20000 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise BuildError(f"Invalid text at {where}")
    if required and not value.strip():
        raise BuildError(f"Empty required text at {where}")
    return value.strip()


def exact_keys(value, allowed, where, required=()):
    if not isinstance(value, dict) or set(value) - allowed or set(required) - set(value):
        raise BuildError(f"Unexpected structure at {where}")


@dataclass
class Dataset:
    entries: list[dict]
    topics: dict[str, str]
    index: dict

    @property
    def counts(self):
        counts = Counter(e["cefr"] for e in self.entries)
        return {level: counts[level] for level in LEVELS}

    @property
    def coverage(self):
        return {kind: sum(bool(e["m"] & bit) for e in self.entries) for kind, bit in MEDIA_BITS.items()}

    @property
    def fingerprint(self):
        return digest({"entries": self.entries, "topics": self.topics})


def load_dataset(root):
    raw = read_json(safe_file(root, "data/word-atlas.json"))
    index = read_json(safe_file(root, "public/atlas/index.json"))
    exact_keys(index, INDEX_KEYS, "index", INDEX_KEYS)
    if index["id"] != "word-atlas-open" or index["version"] != 1 or index["license"] != "CC BY 4.0":
        raise BuildError("Unsupported upstream dataset identity, version or license")
    if index["cefr"] != list(LEVELS) or index["fields"] != ["id", "word", "zh", "ipa", "cefr", "topic", "media"]:
        raise BuildError("The index column/CEFR contract has changed")
    if not isinstance(raw, list) or not raw or not isinstance(index["words"], list):
        raise BuildError("The dataset and index must contain nonempty arrays")
    topics = {}
    if not isinstance(index["topics"], list) or not index["topics"]:
        raise BuildError("Missing topic mapping")
    for pair in index["topics"]:
        if not isinstance(pair, list) or len(pair) != 2:
            raise BuildError("Invalid topic mapping")
        topic = valid_id(pair[0])
        if topic in topics:
            raise BuildError(f"Duplicate topic: {topic}")
        topics[topic] = text(pair[1], "topic label", required=True)
    by_id = {}
    for original in raw:
        exact_keys(original, TEXT_KEYS, "entry", {"id", "w", "senses", "ex", "cefr", "topic"})
        ident = valid_id(original["id"])
        if ident in by_id:
            raise BuildError(f"Duplicate entry ID: {ident}")
        entry = {key: text(original.get(key, ""), f"{ident}.{key}", required=key in {"id", "w", "cefr", "topic"})
                 for key in ("id", "w", "uk", "us", "tip", "cefr", "topic", "note")}
        if entry["cefr"] not in LEVELS or entry["topic"] not in topics:
            raise BuildError(f"Invalid classification for {ident}")
        for key, keys in (("senses", {"pos", "zh", "en"}), ("ex", {"en", "zh"})):
            values = original[key]
            if not isinstance(values, list) or (key == "senses" and not values) or (key == "ex" and len(values) > 2):
                raise BuildError(f"Unsupported {key} array for {ident}")
            entry[key] = []
            for value in values:
                exact_keys(value, keys, f"{ident}.{key}", keys)
                entry[key].append({k: text(value[k], f"{ident}.{key}.{k}", required=k == ("zh" if key == "senses" else "en")) for k in sorted(keys)})
        for key in ("col", "fam", "syn", "ant"):
            values = original.get(key, [])
            if not isinstance(values, list):
                raise BuildError(f"Invalid {key} for {ident}")
            entry[key] = [text(v, f"{ident}.{key}", required=True) for v in values]
        by_id[ident] = entry
    entries = []
    seen = set()
    topic_ids = list(topics)
    for row in index["words"]:
        if not isinstance(row, list) or len(row) != 7:
            raise BuildError("Malformed compact index row")
        ident, word, zh, ipa, level, topic, flags = row
        valid_id(ident)
        if ident in seen or ident not in by_id:
            raise BuildError(f"Duplicate or unknown indexed ID: {ident}")
        if any(type(n) is not int for n in (level, topic, flags)) or not 0 <= flags <= 31:
            raise BuildError(f"Invalid index integers/media flags: {ident}")
        if not 0 <= level < len(LEVELS) or not 0 <= topic < len(topic_ids):
            raise BuildError(f"Invalid index mapping: {ident}")
        entry = by_id[ident]
        first = entry["senses"][0]
        expected = [entry["w"], f'{first["pos"]} {first["zh"]}'.strip(), entry["uk"] or entry["us"], entry["cefr"], entry["topic"]]
        if [word, zh, ipa, LEVELS[level], topic_ids[topic]] != expected:
            raise BuildError(f"Data/index disagreement for {ident}")
        if (flags & 8 and len(entry["ex"]) < 1) or (flags & 16 and len(entry["ex"]) < 2):
            raise BuildError(f"Example recording without its sentence: {ident}")
        entry["m"] = flags
        entries.append(entry)
        seen.add(ident)
    if seen != set(by_id):
        raise BuildError("Data/index ID sets differ")
    actual = {"count": len(entries), "examples": sum(len(e["ex"]) for e in entries),
              "illustrated": sum(bool(e["m"] & 1) for e in entries), "voiced": sum(bool(e["m"] & 2) for e in entries)}
    if any(type(index[k]) is not int or index[k] != v for k, v in actual.items()) or type(index["chunk"]) is not int or index["chunk"] <= 0:
        raise BuildError(f"Index aggregate mismatch; actual values: {actual}")
    return Dataset(entries, topics, index)


def load_identities(path, entries):
    config = read_json(path)
    exact_keys(config, {"aliases", "confirmed_deletions"}, "identity config", {"aliases", "confirmed_deletions"})
    aliases = config["aliases"]
    deletions = config["confirmed_deletions"]
    if not isinstance(aliases, dict) or not isinstance(deletions, list):
        raise BuildError("Invalid identity configuration")
    for key, value in aliases.items():
        valid_id(key)
        valid_id(value)
    for value in deletions:
        valid_id(value)
    if len(deletions) != len(set(deletions)):
        raise BuildError("Duplicate confirmed deletion")

    def resolve(ident):
        seen = set()
        while ident in aliases:
            if ident in seen:
                raise BuildError("Cyclic ID alias")
            seen.add(ident)
            ident = aliases[ident]
        return ident

    for ident in aliases:
        resolve(ident)
    identities = {entry["id"]: resolve(entry["id"]) for entry in entries}
    if len(set(identities.values())) != len(identities):
        raise BuildError("Two current entries resolve to the same canonical ID")
    if set(identities.values()) & set(deletions):
        raise BuildError("A confirmed deletion is still present; explicitly remove its tombstone first")
    return identities, set(deletions)


def media_paths(entries):
    result = {}
    for entry in entries:
        for kind, bit in MEDIA_BITS.items():
            if entry["m"] & bit:
                relative = (f'public/atlas/img/{entry["id"]}.webp' if kind == "img"
                            else f'public/atlas/audio/{kind}/{entry["id"]}.mp3')
                result[(entry["id"], kind)] = relative
    return result
