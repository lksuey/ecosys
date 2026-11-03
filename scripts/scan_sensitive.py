"""Pre-publication scan for secrets and personal data (stdlib only).

Usage:
    python scripts/scan_sensitive.py [ROOT] [--names-file FILE]

Reports only counts and file:line locations, never the matched text.
--names-file points to a local text file (one real business name per line,
kept OUTSIDE the repository) to be searched case-insensitively.
Exit code 1 if anything is found.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}
TEXT_EXT = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".bat", ".html", ".css", ".js",
            ".svg", ".toml", ".cfg", ".ini", ".xml", ".gitignore", ".gitattributes", ""}

PATTERNS = {
    "trello_key_or_token": re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-f]{32}|[0-9a-f]{64})(?![0-9a-fA-F])"),
    "trello_atlassian_token": re.compile(r"ATTA[0-9A-Za-z]{20,}"),
    "trello_object_id": re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{24}(?![0-9a-fA-F])"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)(?:\+?55[\s.-]?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?!\d)"),
    "windows_user_path": re.compile(r"[A-Za-z]:\+Users|/Users/[A-Za-z]|/c/Users", re.I),
    "trello_url": re.compile(r"trello\.com/(?:b|c)/[A-Za-z0-9]+", re.I),
    "maps_place_url": re.compile(r"google\.[a-z.]+/maps/place|maps\.app\.goo\.gl|googleusercontent\.com", re.I),
    "ftid": re.compile(r"\b0x[0-9a-f]{8,}:0x[0-9a-f]{8,}\b", re.I),
    "whatsapp_number_link": re.compile(r"wa\.me/\d{8,}|api\.whatsapp\.com/send\?phone=\d{8,}", re.I),
}
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.invalid", "users.noreply.github.com")


def iter_files(root: Path, self_path: Path):
    for p in root.rglob("*"):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.resolve() == self_path:
            continue
        if p.suffix.lower() in TEXT_EXT or p.name in {".gitignore", ".gitattributes"}:
            yield p


def is_placeholder(kind: str, text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    if kind in {"phone", "whatsapp_number_link"}:
        return "0000000" in digits or digits.count("0") >= 7 or len(set(digits)) <= 2
    if kind == "email":
        return text.lower().endswith(ALLOWED_EMAIL_DOMAINS)
    if kind in {"trello_object_id", "trello_key_or_token"}:
        return len(set(text.lower())) <= 3  # 000...0, 000...1 style placeholders
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--names-file")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    names = []
    if args.names_file:
        names = [n.strip().lower() for n in Path(args.names_file).read_text(encoding="utf-8").splitlines() if n.strip()]
    counts: Counter[str] = Counter()
    hits: list[str] = []
    scanned = 0
    for f in iter_files(root, Path(__file__).resolve()):
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        scanned += 1
        rel = f.relative_to(root).as_posix()
        for i, line in enumerate(lines, 1):
            for kind, rx in PATTERNS.items():
                for m in rx.finditer(line):
                    if is_placeholder(kind, m.group(0)):
                        continue
                    counts[kind] += 1
                    hits.append(f"{rel}:{i} [{kind}]")
            low = line.lower()
            for n in names:
                if n in low:
                    counts["real_business_name"] += 1
                    hits.append(f"{rel}:{i} [real_business_name]")
    print(f"scanned {scanned} text files under {root.name}")
    if not counts:
        print("findings: 0")
        return 0
    print(f"findings: {sum(counts.values())}")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    for h in sorted(set(hits)):
        print(h)
    return 1


if __name__ == "__main__":
    sys.exit(main())
