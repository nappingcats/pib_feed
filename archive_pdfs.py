#!/usr/bin/env python3
"""Mirror current-affairs PDFs into a GitHub Release, so the RSS feeds can link
to durable, renamed copies without bloating the repo or the GitHub Pages site.

Why a Release (not the git tree): the PDFs are large (Vision IAS ~25 MB/doc,
NextIAS ~45 MB/magazine). GitHub Pages published sites are capped at 1 GB and
repos are recommended under 1 GB, but release assets allow up to 2 GB per file
and do NOT count against either — and never enter git history. See DOCS.md.

Inputs: the `archive/<key>.json` manifests written by visioniaspt365.py and
meca.py when run with *_ARCHIVE_MODE=archive. Each is a list of {name, url}:
`name` is the stable archival filename (already renamed), `url` is the source
PDF. This script uploads any manifest entry not already present as an asset on
the release, downloading each PDF straight to a temp file and deleting it after
upload. Already-present assets are skipped, so it is safe to run every build.

Requires the `gh` CLI authenticated with a token that can edit releases
(GITHUB_TOKEN in Actions). Configure via env:
    ARCHIVE_RELEASE_TAG   release tag/name to store assets under (default pdf-archive)
    ARCHIVE_MANIFEST_DIR  where the *.json manifests live (default archive)
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile

import requests

TAG = os.environ.get("ARCHIVE_RELEASE_TAG", "pdf-archive")
MANIFEST_DIR = os.environ.get("ARCHIVE_MANIFEST_DIR", "archive")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"
TIMEOUT = int(os.environ.get("ARCHIVE_TIMEOUT", "120"))
# GitHub rejects release assets over 2 GB; skip anything absurd defensively.
MAX_BYTES = int(os.environ.get("ARCHIVE_MAX_BYTES", str(2 * 1024**3 - 1)))


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def ensure_release() -> None:
    if gh("release", "view", TAG, check=False).returncode == 0:
        return
    print(f"creating release {TAG}")
    gh(
        "release",
        "create",
        TAG,
        "--title",
        "PDF archive",
        "--notes",
        "Archived current-affairs PDFs referenced by the RSS feeds. "
        "Managed automatically by archive_pdfs.py.",
    )


def existing_assets() -> set[str]:
    out = gh("release", "view", TAG, "--json", "assets").stdout
    try:
        data = json.loads(out)
    except ValueError:
        return set()
    return {a["name"] for a in data.get("assets", [])}


def load_manifests() -> dict[str, str]:
    wanted: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(MANIFEST_DIR, "*.json"))):
        try:
            entries = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"  skip {path}: {e}", file=sys.stderr)
            continue
        for e in entries:
            if e.get("name") and e.get("url"):
                wanted.setdefault(e["name"], e["url"])
    return wanted


def upload(name: str, url: str) -> bool:
    try:
        with requests.get(url, stream=True, timeout=TIMEOUT, headers={"User-Agent": UA}) as r:
            if r.status_code != 200:
                print(f"  download HTTP {r.status_code}: {url}", file=sys.stderr)
                return False
            with tempfile.TemporaryDirectory() as td:
                fp = os.path.join(td, name)
                size = 0
                with open(fp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            print(f"  too large, skip: {name}", file=sys.stderr)
                            return False
                        f.write(chunk)
                # clobber lets a re-run replace a partial/failed asset of the same name
                res = gh("release", "upload", TAG, fp, "--clobber", check=False)
                if res.returncode != 0:
                    print(f"  upload failed {name}: {res.stderr.strip()}", file=sys.stderr)
                    return False
                print(f"  + {name} ({size // 1024} KiB)")
                return True
    except requests.RequestException as e:  # pragma: no cover - network
        print(f"  error {name}: {e}", file=sys.stderr)
        return False


def main() -> int:
    wanted = load_manifests()
    if not wanted:
        print("no manifest entries; nothing to archive")
        return 0
    ensure_release()
    have = existing_assets()
    todo = {n: u for n, u in wanted.items() if n not in have}
    print(f"manifest={len(wanted)} present={len(have)} to-upload={len(todo)}")
    ok = 0
    for name, url in todo.items():
        if upload(name, url):
            ok += 1
    print(f"done: uploaded {ok}/{len(todo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
