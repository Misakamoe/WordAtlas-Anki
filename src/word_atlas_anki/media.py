import hashlib
import io
import os
import shutil
import struct
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .constants import ROOT
from .data import media_paths
from .util import BuildError, file_digest, read_json, run, safe_file, write_json


def validate_header(path):
    with path.open("rb") as stream:
        header = stream.read(48)
    size = path.stat().st_size
    if path.suffix == ".webp":
        if len(header) < 30 or header[:4] != b"RIFF" or header[8:12] != b"WEBP" or int.from_bytes(header[4:8], "little") + 8 != size:
            raise BuildError(f"Invalid WebP container: {path}")
        chunk = header[12:16]
        if chunk == b"VP8 ":
            if header[23:26] != b"\x9d\x01\x2a":
                raise BuildError(f"Invalid WebP frame: {path}")
            width, height = (v & 0x3fff for v in struct.unpack("<HH", header[26:30]))
        elif chunk == b"VP8L" and header[20] == 0x2f:
            value = int.from_bytes(header[21:25], "little")
            width, height = (value & 0x3fff) + 1, ((value >> 14) & 0x3fff) + 1
        elif chunk == b"VP8X":
            width, height = int.from_bytes(header[24:27], "little") + 1, int.from_bytes(header[27:30], "little") + 1
            if header[20] & 2:
                raise BuildError(f"Animated illustrations are unsupported: {path}")
        else:
            raise BuildError(f"Unsupported WebP format: {path}")
        if (width, height) != (512, 512):
            raise BuildError(f"Expected the 512px original illustration, got {width}x{height}: {path}")
    elif path.suffix == ".mp3":
        if size < 128 or not (header[:3] == b"ID3" or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)):
            raise BuildError(f"Invalid MP3 container: {path}")
    else:
        raise BuildError(f"Unexpected media type: {path}")


class MediaStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.files = {}
        self.records = {}
        self.static_names = {}

    def add(self, source, *, prefix="wa", kind=None):
        source = Path(source)
        checksum = file_digest(source)
        name = f"{prefix}_{checksum}{source.suffix}"
        if name not in self.records:
            target = self.directory / name
            # Copy instead of linking: source changes cannot mutate a staged artifact.
            shutil.copyfile(source, target)
            if file_digest(target) != checksum:
                raise BuildError(f"Input changed while staging: {source}")
            self.files[name] = target
            self.records[name] = {"sha256": checksum, "bytes": target.stat().st_size, "type": kind or source.suffix[1:]}
        return {"name": name, **self.records[name]}

    def collect(self, root, entries):
        by_entry = {entry["id"]: {} for entry in entries}
        for (ident, kind), relative in media_paths(entries).items():
            source = safe_file(root, relative)
            validate_header(source)
            by_entry[ident][kind] = self.add(source)
        return by_entry

    def add_static(self, project=ROOT):
        for style in ("normal", "italic"):
            original = f"_wa_fraunces_{style}.woff2"
            source = safe_file(project, "assets/" + original)
            with source.open("rb") as stream:
                header = stream.read(48)
            if len(header) != 48 or header[:4] != b"wOF2" or int.from_bytes(header[8:12], "big") != source.stat().st_size:
                raise BuildError(f"Invalid WOFF2 font: {source}")
            self.static_names[original] = self.add(source, prefix=f"_wa_fraunces_{style}", kind="font")["name"]
        content = io.BytesIO()
        with wave.open(content, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0" * 640)
        silence = self.directory / "_wa_stop.wav"
        silence.write_bytes(content.getvalue())
        self.static_names["_wa_stop.wav"] = self.add(silence, prefix="_wa_stop", kind="control")["name"]
        silence.unlink()
        for source in sorted((Path(project) / "licenses").glob("*.txt")):
            source = safe_file(project, source.relative_to(project).as_posix())
            self.add(source, prefix="_wa_license", kind="license")
        self.add(safe_file(project, "LICENSE"), prefix="_wa_converter_license", kind="license")


def decode_media(store, cache=None):
    version = run(["ffmpeg", "-version"]).splitlines()[0]
    cached = read_json(cache) if cache and Path(cache).exists() else {}
    validated = set(cached.get("sha256", [])) if cached.get("decoder") == version else set()
    groups = []
    for suffix in (".webp", ".mp3"):
        paths = [path for name, path in sorted(store.files.items()) if path.suffix == suffix and store.records[name]["sha256"] not in validated]
        groups.extend(paths[start:start + 128] for start in range(0, len(paths), 128))

    def decode(paths):
        with tempfile.TemporaryDirectory(prefix="decode-", dir=store.directory) as temp:
            playlist = Path(temp) / "media.ffconcat"
            lines = ["ffconcat version 1.0"]
            for path in paths:
                quoted = path.resolve().as_posix().replace("'", "'\\''")
                lines.append(f"file '{quoted}'")
            playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
            stream = "0:v:0" if paths[0].suffix == ".webp" else "0:a:0"
            run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-err_detect", "explode", "-threads", "1",
                 "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", playlist,
                 "-map", stream, "-threads", "1", "-f", "null", "-"], timeout=300)
        return [store.records[path.name]["sha256"] for path in paths]

    print(f"Decode validation: {len(groups)} batches; {len(validated)} cached hashes", flush=True)
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
        futures = [pool.submit(decode, paths) for paths in groups]
        for i, future in enumerate(as_completed(futures), 1):
            validated.update(future.result())
            if i % 20 == 0 or i == len(groups):
                print(f"Decoded {i}/{len(groups)} batches", flush=True)
    if cache:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        write_json(cache, {"decoder": version, "sha256": sorted(validated)})
    return {"decoder": version, "validated": sum(path.suffix in (".mp3", ".webp") for path in store.files.values())}
