"""One-time, checksum-verified vendoring. Never executed on upstream updates."""
import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from word_atlas_anki.upstream import license_hashes

PACKAGES = (
    ("https://registry.npmjs.org/@fontsource-variable/fraunces/-/fraunces-5.3.0.tgz",
     "9BYGySn4AHEJdgp9Z28tQ3X+laJMEOITXkQarZXeloWQZDq5oOvXJ3kDA8c7MGIfpogIaZfjrQBqmda8POOCKA==",
     {"package/files/fraunces-latin-wght-normal.woff2": "assets/_wa_fraunces_normal.woff2",
      "package/files/fraunces-latin-wght-italic.woff2": "assets/_wa_fraunces_italic.woff2",
      "package/LICENSE": "licenses/Fraunces-OFL-1.1.txt"}),
    ("https://registry.npmjs.org/lucide-react/-/lucide-react-0.468.0.tgz",
     "6koYRhnM2N0GGZIdXzSeiNwguv1gt/FAjZOiPl76roBi3xKEXa4WmfpxgQwTTL4KipXjefrnf3oV4IsYhi4JFA==",
     {"package/LICENSE": "licenses/Lucide-ISC.txt"}),
)


def download(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "WordAtlas-Anki asset vendor"})
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read(50 * 1024 * 1024 + 1)
                if len(data) > 50 * 1024 * 1024:
                    raise ValueError("Asset exceeds download limit")
                return data
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--reviewed-sha", required=True)
    args = parser.parse_args()
    provenance = {}
    for url, checksum, members in PACKAGES:
        data = download(url)
        if base64.b64encode(hashlib.sha512(data).digest()).decode() != checksum:
            raise ValueError(f"Package integrity mismatch: {url}")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member, target in members.items():
                info = archive.getmember(member)
                if not info.isfile() or info.size > 2 * 1024 * 1024:
                    raise ValueError(f"Unsafe package member: {member}")
                content = archive.extractfile(info).read()
                output = ROOT / target
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
                provenance[target] = {"url": url, "member": member, "sha256": hashlib.sha256(content).hexdigest()}
    for source, target in (("LICENSE", "Word-Atlas-MIT.txt"), ("CONTENT_LICENSE.md", "Word-Atlas-content.txt")):
        (ROOT / "licenses" / target).write_text((args.upstream / source).read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    legal_url = "https://creativecommons.org/licenses/by/4.0/legalcode.txt"
    content = download(legal_url)
    if b"Attribution 4.0 International" not in content or b"<html" in content.lower():
        raise ValueError("Unexpected Creative Commons legal text")
    (ROOT / "licenses/CC-BY-4.0.txt").write_bytes(content)
    provenance["licenses/CC-BY-4.0.txt"] = {"url": legal_url, "sha256": hashlib.sha256(content).hexdigest()}
    (ROOT / "config/upstream-licenses.json").write_text(json.dumps({"reviewed_sha": args.reviewed_sha, "files": license_hashes(args.upstream)}, indent=2) + "\n", encoding="utf-8")
    (ROOT / "assets/provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
