"""Add PyPI's published artifact hashes to pip-compile's exact version pins.

This avoids downloading wheels for hundreds of unrelated architectures just to
compute the same hashes. pip still verifies every installed artifact itself.
"""
import argparse
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    pins = []
    for line in args.pins.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+==[a-zA-Z0-9_.+-]+", line):
            raise ValueError(f"Expected a plain, exact version pin: {line}")
        pins.append(line)

    def hashed(pin):
        name, version = pin.split("==")
        request = urllib.request.Request(f"https://pypi.org/pypi/{name}/{version}/json", headers={"User-Agent": "WordAtlas-Anki lock helper"})
        with urllib.request.urlopen(request, timeout=90) as response:
            package = json.load(response)
        if package["info"]["version"] != version:
            raise ValueError(f"Version mismatch: {pin}")
        hashes = sorted({artifact["digests"]["sha256"] for artifact in package["urls"] if not artifact["yanked"]})
        if not hashes or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
            raise ValueError(f"Missing or invalid hashes: {pin}")
        return pin + " \\\n" + " \\\n".join("    --hash=sha256:" + value for value in hashes)

    with ThreadPoolExecutor(max_workers=4) as pool:
        locked = list(pool.map(hashed, sorted(pins)))
    args.out.write_text("# Exact build/test pins; refresh with pip-compile then tools/hash_lock.py.\n" + "\n".join(locked) + "\n", encoding="utf-8", newline="\n")
    print(f"Locked {len(pins)} distributions with published SHA-256 hashes")


if __name__ == "__main__":
    main()
