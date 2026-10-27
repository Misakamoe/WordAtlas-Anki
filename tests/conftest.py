import copy
import shutil
from pathlib import Path

import pytest
from anki.collection import Collection, ImportAnkiPackageOptions, ImportAnkiPackageRequest

from word_atlas_anki.builder import build
from word_atlas_anki.constants import LEVELS, ROOT
from word_atlas_anki.util import run, write_json

SHA = "a" * 40
NOW = 1793026800


def entries():
    result = []
    for i, ident in enumerate(("alpha", "beta", "gamma", "delta", "epsilon", "zeta")):
        result.append({"id": ident, "w": ident, "uk": "/test-uk/", "us": "/test-us/", "cefr": LEVELS[i], "topic": "education",
                       "senses": [{"pos": "n.", "zh": "含义一", "en": "First complete meaning."}, {"pos": "v.", "zh": "含义二", "en": "Second complete meaning."}],
                       "ex": [{"en": f"We use {ident} every day.", "zh": "第一句译文。"}, {"en": f"This {ident} illustrates the longer context.", "zh": "第二句译文。"}],
                       "col": [f"{ident} example｜中文搭配"], "fam": [ident + "s (n.)"], "syn": ["related"], "ant": ["opposite"],
                       "tip": "记忆提示。", "note": "完整用法提醒。", "m": 31 if i == 0 else 15})
    result[0]["us"] = result[0]["uk"]
    result[2]["uk"] = ""
    result[3]["us"] = ""
    result[3]["m"] = 1
    result[4]["w"] = "pneumonoultramicroscopicsilicovolcanoconiosis"
    result[4]["ex"][1]["en"] = ("Long but readable context with all details preserved. " * 18) + result[4]["w"]
    for key in ("col", "fam", "syn", "ant", "ex"):
        result[5][key] = []
    result[5]["tip"] = result[5]["note"] = ""
    result[5]["m"] = 6
    return result


def write_dataset(root, values):
    root = Path(root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "public/atlas").mkdir(parents=True, exist_ok=True)
    topics = [["education", "教育"], ["environment", "环境"]]
    rows = []
    for e in values:
        sense = e["senses"][0]
        rows.append([e["id"], e["w"], f'{sense["pos"]} {sense["zh"]}'.strip(), e["uk"] or e["us"], LEVELS.index(e["cefr"]),
                     [t[0] for t in topics].index(e["topic"]), e["m"]])
    index = {"id": "word-atlas-open", "version": 1, "name": "Word Atlas Open", "license": "CC BY 4.0", "count": len(values),
             "examples": sum(len(e["ex"]) for e in values), "illustrated": sum(bool(e["m"] & 1) for e in values),
             "voiced": sum(bool(e["m"] & 2) for e in values), "chunk": 100, "topics": topics, "cefr": list(LEVELS),
             "fields": ["id", "word", "zh", "ipa", "cefr", "topic", "media"], "words": rows}
    write_json(root / "data/word-atlas.json", [{k: v for k, v in e.items() if k != "m"} for e in values])
    write_json(root / "public/atlas/index.json", index)


@pytest.fixture(scope="session")
def media_seed(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-media")
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0xc4573a:s=512x512", "-frames:v", "1", root / "image.webp"])
    for i, kind in enumerate(("uk", "us", "ex", "ex2", "replacement")):
        run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={440 + i * 100}:duration=0.15", "-c:a", "libmp3lame", root / (kind + ".mp3")])
    return root


def fixture_upstream(root, media_seed):
    values = entries()
    write_dataset(root, values)
    for source, target in (("Word-Atlas-MIT.txt", "LICENSE"), ("Word-Atlas-content.txt", "CONTENT_LICENSE.md")):
        shutil.copyfile(ROOT / "licenses" / source, root / target)
    for entry in values:
        for kind, bit in (("img", 1), ("uk", 2), ("us", 4), ("ex", 8), ("ex2", 16)):
            if entry["m"] & bit:
                path = root / (f'public/atlas/img/{entry["id"]}.webp' if kind == "img" else f'public/atlas/audio/{kind}/{entry["id"]}.mp3')
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(media_seed / ("image.webp" if kind == "img" else kind + ".mp3"), path)
    return root, values


@pytest.fixture
def upstream(tmp_path, media_seed):
    return fixture_upstream(tmp_path / "upstream", media_seed)


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    for name in ("templates", "assets", "licenses", "config", "src"):
        shutil.copytree(ROOT / name, path / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for name in ("pyproject.toml", "requirements.lock", "LICENSE"):
        shutil.copyfile(ROOT / name, path / name)
    return path


@pytest.fixture(scope="session")
def built(tmp_path_factory, media_seed):
    base = tmp_path_factory.mktemp("built-fixture")
    upstream, values = fixture_upstream(base / "upstream", media_seed)
    out = base / "dist"
    manifest = build(upstream, SHA, out, now=NOW, cache=base / "cache", verify_upstream=False)
    return out, manifest, values


def import_package(collection, path, *, always=False):
    result = collection.import_anki_package(ImportAnkiPackageRequest(
        package_path=str(Path(path).resolve()),
        options=ImportAnkiPackageOptions(update_notes=1 if always else 0, update_notetypes=0,
                                        with_scheduling=False, with_deck_configs=False, merge_notetypes=False)))
    # The GUI invalidates this Python cache after the Rust import operation.
    collection.models._clear_cache()
    return result


def import_dist(collection, out, manifest, *, always=False):
    for part in manifest["packages"]:
        import_package(collection, out / part["name"], always=always)


@pytest.fixture
def collection(tmp_path):
    col = Collection(str(tmp_path / "isolated.anki2"))
    try:
        yield col
    finally:
        col.close()
