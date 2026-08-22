"""Fail closed on accidental expansion of the public Audit Edition tree."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    ".gitattributes",
    ".gitignore",
    ".github/ISSUE_TEMPLATE/claim-falsification.yml",
    "CLAIMS.json",
    "docs/adversarial-review-0.1.1.md",
    "docs/release-0.1.2.md",
    "docs/release-0.1.3.md",
    "KNOWN_LIMITATIONS.md",
    "MANIFEST.in",
    "LICENSE",
    "Makefile",
    "NOTICE.md",
    "README.md",
    "SECURITY.md",
    "SOURCE_MAP.md",
    "pyproject.toml",
}
ROOT_FILES = REQUIRED | {"LICENSE"}
ALLOWED_PREFIXES = (
    ".github/ISSUE_TEMPLATE/",
    ".github/workflows/",
    "docs/",
    "scripts/",
    "src/nexora_audit/",
    "tests/",
)
ALLOWED_SUFFIXES = {".json", ".md", ".py", ".toml", ".yml"}
DENIED_SUFFIXES = {
    ".7z",
    ".csv",
    ".db",
    ".env",
    ".geojson",
    ".gz",
    ".key",
    ".p12",
    ".pem",
    ".sqlite",
    ".tar",
    ".zip",
}
DENIED_NAMES = {".orig", ".rej", ".DS_Store"}
FORBIDDEN_TEXT = (
    re.compile(rb"/" rb"home/[A-Za-z0-9_.-]+/"),
    re.compile(rb"/" rb"mnt/c/Users/[A-Za-z0-9_.-]+/", re.IGNORECASE),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
LICENSE_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"


def tracked_files() -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    tracked: list[tuple[str, str]] = []
    for record in result.stdout.decode("utf-8").split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode = metadata.split(" ", 1)[0]
        tracked.append((mode, path))
    return tracked


def main() -> int:
    failures: list[str] = []
    tracked = tracked_files()
    paths = {path for _mode, path in tracked}

    missing = sorted(REQUIRED - paths)
    if missing:
        failures.append(f"missing required paths: {missing}")

    for mode, path_text in tracked:
        path = Path(path_text)
        if mode != "100644":
            failures.append(f"{path_text}: expected non-executable regular mode 100644, got {mode}")
        if (
            path.name in DENIED_NAMES
            or path.suffix.lower() in DENIED_SUFFIXES
            or any(part.startswith(".") for part in path.parts[1:])
        ):
            failures.append(f"{path_text}: disallowed public path")
        if path_text not in ROOT_FILES and not path_text.startswith(ALLOWED_PREFIXES):
            failures.append(f"{path_text}: outside the public allowlist")
        if path_text not in ROOT_FILES and path.suffix not in ALLOWED_SUFFIXES:
            failures.append(f"{path_text}: disallowed file type")

        absolute = ROOT / path
        content = absolute.read_bytes()
        if len(content) > 1_000_000:
            failures.append(f"{path_text}: file exceeds 1 MB")
        if b"\r\n" in content:
            failures.append(f"{path_text}: CRLF line endings")
        if b"\x00" in content:
            failures.append(f"{path_text}: NUL byte")
        for pattern in FORBIDDEN_TEXT:
            if pattern.search(content):
                failures.append(f"{path_text}: contains forbidden private residue")

    license_path = ROOT / "LICENSE"
    if license_path.is_file():
        observed = hashlib.sha256(license_path.read_bytes()).hexdigest()
        if observed != LICENSE_SHA256:
            failures.append(f"LICENSE: canonical AGPL-3.0 text hash mismatch ({observed})")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print(f"Public tree verified: {len(tracked)} tracked regular files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
