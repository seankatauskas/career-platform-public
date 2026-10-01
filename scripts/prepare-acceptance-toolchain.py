#!/usr/bin/env python3
"""Fetch verified public Linux Tectonic and build a minimal deterministic TeX bundle."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
import urllib.request
import zipfile

TEX_URL = "https://data1b.fullyjustified.net/tlextras-2022.0r0.tar"
TECTONIC_URL = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-unknown-linux-musl.tar.gz"
ARCHIVE_SHA = "dfb82876f2986862996e564fa507a9e576e0c1e3bee63c2c1bd677c2543e6407"
BINARY_SHA = "4df19452c202c5bef9f7c7e4a01a3f2b9d5199f0a1f73b70b4fe1bffbc9837f6"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fetch(url, maximum, byte_range=None):
    headers = {"User-Agent": "career-platform-offline-acceptance-toolchain/1", "Accept-Encoding": "identity"}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1] - 1}"
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as response:
                if byte_range is not None:
                    expected = f"bytes {byte_range[0]}-{byte_range[1] - 1}/"
                    if response.status != 206 or not response.headers.get("Content-Range", "").startswith(expected):
                        raise ValueError("public bundle server did not honor the bounded range")
                result = response.read(maximum + 1)
                if len(result) > maximum:
                    raise ValueError("download exceeded its bound")
                return result
        except Exception as error:
            last = error
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"verified toolchain download failed: {last}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".cache/acceptance-toolchain"))
    parser.add_argument("--seed-bundle", type=Path, help="optional existing ZIP; every cached file is still SHA256-verified")
    args = parser.parse_args()
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    manifest = Path(__file__).with_name("acceptance-tex-files.tsv")
    entries = []
    for line in manifest.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, offset, size, sha = line.split("\t")
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("invalid bundle filename")
        entries.append({"name": name, "offset": int(offset), "size": int(size), "sha256": sha})
    binary = target / "tectonic"
    if not binary.exists() or digest(binary.read_bytes()) != BINARY_SHA:
        archive = fetch(TECTONIC_URL, 16 * 1024 * 1024)
        if digest(archive) != ARCHIVE_SHA:
            raise ValueError("Tectonic release archive SHA256 mismatch")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as contents:
            member = contents.getmember("tectonic")
            if not member.isfile() or member.size > 64 * 1024 * 1024:
                raise ValueError("invalid release binary")
            value = contents.extractfile(member).read()
        if digest(value) != BINARY_SHA:
            raise ValueError("Tectonic executable SHA256 mismatch")
        if binary.exists():
            binary.chmod(0o600)
        binary.write_bytes(value)
    binary.chmod(0o555)
    files = {}
    seed = args.seed_bundle or (target / "tectonic.bundle")
    if seed.exists():
        with zipfile.ZipFile(seed) as archive:
            for entry in entries:
                try:
                    if archive.getinfo(entry["name"]).file_size != entry["size"]:
                        continue
                    value = archive.read(entry["name"])
                except KeyError:
                    continue
                if len(value) == entry["size"] and digest(value) == entry["sha256"]:
                    files[entry["name"]] = value
    missing = sorted((entry for entry in entries if entry["name"] not in files), key=lambda entry: entry["offset"])
    groups = []
    for entry in missing:
        end = entry["offset"] + entry["size"]
        if groups and entry["offset"] - groups[-1]["end"] < 65536 and end - groups[-1]["start"] < 2 * 1024 * 1024:
            groups[-1]["end"] = end
            groups[-1]["entries"].append(entry)
        else:
            groups.append({"start": entry["offset"], "end": end, "entries": [entry]})

    def get_group(group):
        data = fetch(TEX_URL, group["end"] - group["start"], (group["start"], group["end"]))
        values = {}
        for entry in group["entries"]:
            begin = entry["offset"] - group["start"]
            value = data[begin:begin + entry["size"]]
            if len(value) != entry["size"] or digest(value) != entry["sha256"]:
                raise ValueError("public TeX file SHA256 mismatch: " + entry["name"])
            values[entry["name"]] = value
        return values

    with ThreadPoolExecutor(max_workers=4) as executor:
        for values in executor.map(get_group, groups):
            files.update(values)
    # This curated subset has its own identity; do not borrow the full upstream
    # bundle's cache key. Tectonic requires this file inside ZIP bundles.
    files["SHA256SUM"] = (digest(manifest.read_bytes()) + "\n").encode("ascii")
    bundle = target / "tectonic.bundle"
    if bundle.exists():
        bundle.chmod(0o600)
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name])
    bundle.chmod(0o444)
    receipt = {"version": 1, "tectonic_version": "0.15.0", "platform": "linux/amd64", "binary_url": TECTONIC_URL,
               "binary_sha256": BINARY_SHA, "tex_source": TEX_URL, "tex_manifest_sha256": digest(manifest.read_bytes()),
               "tex_files": len(files), "bundle_sha256": digest(bundle.read_bytes())}
    (target / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
