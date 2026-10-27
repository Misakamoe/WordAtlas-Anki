import shutil
import re
from contextlib import closing
from pathlib import Path

import pytest
from anki.collection import Collection, ExportAnkiPackageOptions

from conftest import NOW, SHA, import_dist, import_package, write_dataset
from word_atlas_anki.builder import build
from word_atlas_anki.constants import DECK_DESCRIPTION, MODEL_ID
from word_atlas_anki.util import file_digest

pytestmark = pytest.mark.integration


def test_native_import_repeat_audio_and_export(built, collection, tmp_path):
    out, manifest, _ = built
    import_dist(collection, out, manifest)
    import_dist(collection, out, manifest)
    assert collection.note_count() == 6
    assert len(collection.find_cards("")) == 6
    for ident, record in manifest["notes"].items():
        nid = collection.db.scalar("SELECT id FROM notes WHERE guid=?", record["guid"])
        card = collection.get_note(nid).cards()[0]
        question = [tag.filename for tag in card.question_av_tags()]
        answer = [tag.filename for tag in card.answer_av_tags()]
        control = [name for name in manifest["media"] if name.startswith("_wa_stop_")]
        assert [name for name in question if name not in control] == [record["media"][kind] for kind in ("uk",) if kind in record["media"]]
        assert [name for name in answer if name not in control] == [record["media"][kind] for kind in ("uk", "ex") if kind in record["media"]]
        assert set(control) <= set(question) and set(control) <= set(answer)
        assert 'data-accent="uk"' in card.question() and 'data-accent="us"' in card.question()
        assert 'class="wa-illustration"' not in card.question()
        assert "含义一" not in card.question()
        assert "含义一" in card.answer() and "含义二" in card.answer()
        assert 'class="wa-source"' not in card.answer()
        assert "Word Atlas contributors" not in re.sub(r"<style>.*?</style>", "", card.answer(), flags=re.S)
        assert "Word Atlas contributors" in collection.get_note(nid)["Source"]
    check = collection.media.check()
    assert not list(check.missing) and not list(check.unused)
    exported = tmp_path / "reexport.apkg"
    collection.export_anki_package(out_path=str(exported), options=ExportAnkiPackageOptions(with_media=True, with_scheduling=False, with_deck_configs=False, legacy=True), limit=None)
    with closing(Collection(str(tmp_path / "reimport.anki2"))) as again:
        import_package(again, exported)
        assert again.note_count() == 6
        names = {path.name for path in Path(again.media.dir()).iterdir() if path.is_file()}
        required_media = {name for name, record in manifest["media"].items() if record["type"] != "license"}
        assert required_media <= names
        # Anki omits unreferenced text media; CSS comments retain the notices.
        deck = again.decks.by_name("Word Atlas")
        assert deck["desc"] == DECK_DESCRIPTION
        css = again.models.get(MODEL_ID)["css"]
        assert "Permission is hereby granted" in css and "SIL OPEN FONT LICENSE" in css
        assert "Word Atlas contributors" in css and "CC BY 4.0" in css
        assert not list(again.media.check().missing)


def test_upgrade_preserves_reviews_suspension_move_and_checks_tags(built, upstream, project, media_seed, collection, tmp_path):
    before, original, _ = built
    root, values = upstream
    import_dist(collection, before, original)
    guid = original["notes"]["alpha"]["guid"]
    nid = collection.db.scalar("SELECT id FROM notes WHERE guid=?", guid)
    note = collection.get_note(nid)
    note.tags.append("my_personal_tag")
    collection.update_note(note)
    card = note.cards()[0]
    card.did = collection.decks.id("Personal::Keep")
    card.type, card.queue, card.reps, card.lapses, card.ivl, card.due = 2, -1, 9, 2, 30, 123
    collection.update_card(card)
    collection.db.execute("INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?,?)", NOW * 1000, card.id, -1, 3, 30, 15, 2500, 900, 1)
    state = (card.id, card.did, card.type, card.queue, card.reps, card.lapses, card.ivl, card.due)
    values[0]["senses"][0]["zh"] = "更新后的含义"
    values[0]["cefr"], values[0]["topic"] = "B2", "environment"
    write_dataset(root, values)
    shutil.copyfile(media_seed / "replacement.mp3", root / "public/atlas/audio/uk/alpha.mp3")
    with (project / "templates/card.css").open("a", encoding="utf-8") as stream:
        stream.write("\n.wa-card { border-width: 2px; }\n")
    # Explicitly make source changes newer than the simulated local tag edit.
    collection.db.execute("UPDATE notes SET mod=? WHERE id=?", NOW, nid)
    after_dir = tmp_path / "upgrade"
    after = build(root, SHA, after_dir, previous_manifest=before / "manifest.json", project=project, cache=tmp_path / "cache", now=NOW + 100, verify_upstream=False)
    import_dist(collection, after_dir, after)
    assert collection.note_count() == 6 and len(collection.find_cards("")) == 6
    changed = collection.get_note(nid)
    current = changed.cards()[0]
    assert (current.id, current.did, current.type, current.queue, current.reps, current.lapses, current.ivl, current.due) == state
    assert collection.db.scalar("SELECT count(*) FROM revlog WHERE cid=?", card.id) == 1
    assert "更新后的含义" in changed["Senses"] and changed["CEFR"] == "B2"
    # Anki 26.9.3 replaces tags on an updated note instead of merging them.
    assert set(changed.tags) == {"word_atlas", "word_atlas::cefr::B2", "word_atlas::topic::environment"}
    assert ".wa-card { border-width: 2px; }" in collection.models.get(MODEL_ID)["css"]
    assert len([m for m in collection.models.all_names_and_ids() if m.name.startswith("Word Atlas - Offline")]) == 1
    new_media = after["notes"]["alpha"]["media"]["uk"]
    assert new_media != original["notes"]["alpha"]["media"]["uk"]
    assert file_digest(Path(collection.media.dir()) / new_media) == after["media"][new_media]["sha256"]
    assert all(after["notes"][ident]["mod"] == original["notes"][ident]["mod"] for ident in original["notes"] if ident != "alpha")


def test_local_edit_respects_native_update_option(built, collection):
    out, manifest, _ = built
    import_dist(collection, out, manifest)
    nid = collection.db.scalar("SELECT id FROM notes WHERE guid=?", manifest["notes"]["alpha"]["guid"])
    note = collection.get_note(nid)
    note["Tip"] = "My newer local edit"
    collection.update_note(note)
    collection.db.execute("UPDATE notes SET mod=? WHERE id=?", NOW + 10000, nid)
    import_dist(collection, out, manifest)
    assert collection.get_note(nid)["Tip"] == "My newer local edit"
    import_dist(collection, out, manifest, always=True)
    assert collection.get_note(nid)["Tip"] == "记忆提示。"


def test_existing_deck_receives_concise_description(built, collection):
    out, manifest, _ = built
    import_dist(collection, out, manifest)
    deck = collection.decks.by_name("Word Atlas")
    assert deck["desc"] == DECK_DESCRIPTION
    deck["desc"] = "Previous verbose description"
    deck["mod"] = NOW - 100
    collection.decks.update(deck, preserve_usn=True)
    import_dist(collection, out, manifest)
    # Anki can remap deck IDs on import; resolve the imported deck by name.
    assert collection.decks.by_name("Word Atlas")["desc"] == DECK_DESCRIPTION


def test_sample_to_full_does_not_duplicate(upstream, built, collection, tmp_path):
    root, _ = upstream
    sample_dir = tmp_path / "sample"
    sample = build(root, SHA, sample_dir, sample=2, now=NOW - 10, cache=tmp_path / "cache", verify_upstream=False)
    import_dist(collection, sample_dir, sample)
    assert collection.note_count() == 2
    full_dir, full, _ = built
    import_dist(collection, full_dir, full)
    assert collection.note_count() == 6 and len(collection.find_cards("")) == 6


def test_initial_positions_follow_index_not_deck_iteration(upstream, collection, tmp_path):
    root, values = upstream
    write_dataset(root, list(reversed(values)))
    out = tmp_path / "reordered"
    manifest = build(root, SHA, out, now=NOW, cache=tmp_path / "cache", verify_upstream=False)
    import_dist(collection, out, manifest)
    positions = []
    for record in manifest["notes"].values():
        nid = collection.db.scalar("SELECT id FROM notes WHERE guid=?", record["guid"])
        positions.append((collection.get_note(nid).cards()[0].due, record["order"]))
    assert [order for _, order in sorted(positions)] == list(range(6))


def test_oversize_cefr_shards_are_independently_importable(upstream, collection, tmp_path):
    root, values = upstream
    for entry in values:
        entry["cefr"] = "A1"
    write_dataset(root, values)
    original = build(root, SHA, tmp_path / "unsplit", now=NOW, cache=tmp_path / "cache", verify_upstream=False)
    out = tmp_path / "split"
    ceiling = original["packages"][0]["bytes"] - 1
    manifest = build(root, SHA, out, now=NOW, cache=tmp_path / "cache", verify_upstream=False, package_limit=ceiling)
    assert len(manifest["packages"]) >= 2
    assert all(part["bytes"] < ceiling and "A1-" in part["name"] for part in manifest["packages"])
    for part in reversed(manifest["packages"]):
        import_package(collection, out / part["name"])
    import_dist(collection, out, manifest)
    assert collection.note_count() == 6 and len(collection.find_cards("")) == 6
    assert not list(collection.media.check().missing)
