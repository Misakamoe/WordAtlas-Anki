import argparse
import json
import sys
from pathlib import Path

from .util import BuildError, write_json


def main():
    parser = argparse.ArgumentParser(description="Build and verify offline Word Atlas Anki decks")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Fetch pinned inputs, never execute upstream code")
    fetch.add_argument("--upstream", type=Path, required=True)
    fetch.add_argument("--upstream-sha")
    fetch.add_argument("--media", action="store_true")
    fetch.add_argument("--sample", type=int)
    validate = commands.add_parser("validate", help="Cross-check the complete data and compact index")
    validate.add_argument("--upstream", type=Path, required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--upstream", type=Path, required=True)
    build_parser.add_argument("--upstream-sha", required=True)
    build_parser.add_argument("--out", type=Path, default=Path("dist"))
    build_parser.add_argument("--previous-manifest", type=Path)
    build_parser.add_argument("--sample", type=int)
    build_parser.add_argument("--cache", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--out", type=Path, default=Path("dist"))
    verify.add_argument("--release", action="store_true")
    verify.add_argument("--native", action="store_true", help="Also test the official Anki backend in temporary collections")
    verify.add_argument("--report", type=Path)
    check = commands.add_parser("release-check")
    check.add_argument("--repo", required=True)
    check.add_argument("--upstream", type=Path, default=Path(".cache/upstream"))
    check.add_argument("--state", type=Path, default=Path(".cache/release-state.json"))
    release_build = commands.add_parser("release-build")
    release_build.add_argument("--state", type=Path, required=True)
    release_build.add_argument("--out", type=Path, default=Path("dist"))
    publish = commands.add_parser("publish")
    publish.add_argument("--repo", required=True)
    publish.add_argument("--out", type=Path, default=Path("dist"))
    publish.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command in ("fetch", "validate"):
            from .data import load_dataset
            if args.command == "fetch":
                from .upstream import checkout_metadata, hydrate_media
                sha = checkout_metadata(args.upstream, args.upstream_sha)
                if args.media:
                    from .builder import select_sample
                    entries = load_dataset(args.upstream).entries
                    if args.sample is not None:
                        entries = select_sample(entries, args.sample)
                    hydrate_media(args.upstream, sha, entries, full=args.sample is None)
                print(sha)
            else:
                dataset = load_dataset(args.upstream)
                print(json.dumps({"count": len(dataset.entries), "levels": dataset.counts, "coverage": dataset.coverage}, indent=2))
        elif args.command == "build":
            from .builder import build
            build(args.upstream, args.upstream_sha, args.out, previous_manifest=args.previous_manifest, sample=args.sample, cache=args.cache)
        elif args.command == "verify":
            from .verify import verify_dist
            manifest = verify_dist(args.out, release=args.release)
            print(f"Verified {manifest['note_count']} notes across {len(manifest['packages'])} packages")
            if args.native:
                from .anki_verify import verify_native
                print(json.dumps(verify_native(args.out, args.report), ensure_ascii=False, indent=2))
        elif args.command == "release-check":
            from .publication import check_release
            check_release(args.repo, args.upstream, args.state)
        elif args.command == "release-build":
            from .publication import build_release
            build_release(args.state, args.out)
        elif args.command == "publish":
            from .publication import publish_release
            publish_release(args.repo, args.out, args.state)
    except (BuildError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
