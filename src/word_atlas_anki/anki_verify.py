"""Official Anki backend checks, not a desktop/mobile GUI compatibility claim."""
import importlib.metadata
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from .constants import DECK_DESCRIPTION, MODEL_ID
from .util import BuildError, write_json
from .verify import verify_dist


def verify_native(directory, report_path=None):
    from anki.collection import Collection, ExportAnkiPackageOptions, ImportAnkiPackageOptions, ImportAnkiPackageRequest

    directory = Path(directory).resolve()
    manifest = verify_dist(directory)

    def import_one(col, path):
        col.import_anki_package(ImportAnkiPackageRequest(package_path=str(path), options=ImportAnkiPackageOptions(
            update_notes=0, update_notetypes=0, with_scheduling=False, with_deck_configs=False)))

    def check_counts(col):
        if col.note_count() != manifest["note_count"] or len(col.find_cards("")) != manifest["note_count"]:
            raise BuildError("Official Anki import produced missing or duplicate notes/cards")
        if col.db.scalar("SELECT count(*) FROM revlog"):
            raise BuildError("Official import unexpectedly contains review history")
        media = col.media.check()
        if list(media.missing) or list(media.unused):
            raise BuildError(f"Official media check failed: missing={list(media.missing)[:5]}, unused={list(media.unused)[:5]}")

    report = {"anki_backend_version": importlib.metadata.version("anki"), "platform": sys.platform,
              "scope": "Official backend only; no Anki GUI, AnkiDroid or AnkiMobile device claim",
              "upstream_sha": manifest["upstream_sha"], "input_fingerprint": manifest["input_fingerprint"],
              "note_count": manifest["note_count"], "card_count": manifest["note_count"],
              "media_count": manifest["media_count"]}
    with tempfile.TemporaryDirectory(prefix="wa-native-verify-") as temporary:
        temp = Path(temporary)
        exported = temp / "round-trip.apkg"
        print("Official Anki: importing and repeating the complete candidate", flush=True)
        with closing(Collection(str(temp / "first.anki2"))) as col:
            for _ in range(2):
                for part in manifest["packages"]:
                    import_one(col, directory / part["name"])
                check_counts(col)
            by_guid = dict(col.db.all("SELECT guid,id FROM notes"))
            by_note = dict(col.db.all("SELECT nid,id FROM cards"))
            controls = [name for name in manifest["media"] if name.startswith("_wa_stop_")]
            if len(controls) != 1:
                raise BuildError("Expected one packaged silent queue control")
            print("Official Anki: checking both audio queues for every note", flush=True)
            for ident, record in manifest["notes"].items():
                if record["guid"] not in by_guid:
                    raise BuildError(f"Imported GUID was lost: {ident}")
                card = col.get_card(by_note[by_guid[record["guid"]]])
                question = [getattr(tag, "filename", None) for tag in card.question_av_tags()]
                answer = [getattr(tag, "filename", None) for tag in card.answer_av_tags()]
                expected_question = [record["media"][kind] for kind in ("uk",) if kind in record["media"]] + controls
                expected_answer = [record["media"][kind] for kind in ("uk", "ex") if kind in record["media"]] + controls
                if question != expected_question or answer != expected_answer:
                    raise BuildError(f"Unexpected native audio queue: {ident}")
                if any(f'data-accent="{accent}"' not in card.question() or f'data-accent="{accent}"' not in card.answer() for accent in ("uk", "us")):
                    raise BuildError(f"Both IPA slots must survive actual rendering: {ident}")
            print("Official Anki: exporting all media for round-trip verification", flush=True)
            col.export_anki_package(out_path=str(exported), options=ExportAnkiPackageOptions(
                with_media=True, with_scheduling=False, with_deck_configs=False, legacy=True), limit=None)
        with closing(Collection(str(temp / "second.anki2"))) as col:
            import_one(col, exported)
            check_counts(col)
            available = {path.name for path in Path(col.media.dir()).iterdir() if path.is_file()}
            required = {name for name, record in manifest["media"].items() if record["type"] != "license"}
            if not required <= available:
                raise BuildError(f"Re-export lost required media: {sorted(required - available)[:5]}")
            if col.decks.by_name("Word Atlas")["desc"] != DECK_DESCRIPTION:
                raise BuildError("Re-export changed the concise deck description")
            if "Permission is hereby granted" not in col.models.get(MODEL_ID)["css"]:
                raise BuildError("Re-export lost the bundled note-type license notices")
            report["round_trip_required_media"] = len(required)
    report.update(repeat_import=True, all_native_queues=True, media_check=True, media_round_trip=True)
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        write_json(report_path, report)
    print("Official Anki backend verification passed", flush=True)
    return report
