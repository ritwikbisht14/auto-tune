#!/usr/bin/env python3
"""Maintain a list of trusted GitHub authors based on what the user has
manually installed via the skill-lock.json registry.

Premise: when the user installs a skill by hand (find-skills, `claude skill
add`, manual symlink), they've already vetted the source. Whoever authored
it gets added to a `trusted_authors` allowlist that:

  1. boosts that author's other repos in future auto-tune discoveries
  2. seeds curators.txt with the repo's releases.atom feed so future
     versions surface through RSS

Two commands:
  sync   parse ~/.agents/.skill-lock.json -> trusted_authors.txt + curators.txt
  list   print the current trusted authors and repos
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HOME = Path.home()
SKILL_LOCK = HOME / ".agents" / ".skill-lock.json"
SKILL_ROOT = Path(__file__).resolve().parent.parent
SECURITY_DIR = SKILL_ROOT / "security"
TRUSTED_AUTHORS = SECURITY_DIR / "trusted_authors.txt"
TRUSTED_REPOS = SECURITY_DIR / "trusted_repos.txt"
CURATORS = SECURITY_DIR / "curators.txt"

HEADER = (
    "# auto-tune trusted-authors list (auto-maintained by trusted.py)\n"
    "# Authors whose other public repos auto-tune trusts as discovery candidates.\n"
    "# Derived from manual skill installs recorded in ~/.agents/.skill-lock.json.\n"
    "# One GitHub user/org per line. Lines starting with # are comments.\n"
    "# Edit by hand: anything not in a `# auto:` block is preserved.\n"
)

AUTO_BLOCK_START = "# auto: begin synced entries"
AUTO_BLOCK_END = "# auto: end synced entries"


def parse_author_repo(entry: dict) -> tuple[str, str] | None:
    """Return (author, repo_full_name) from a skill-lock entry, or None."""
    src = (entry.get("source") or "").strip()
    src_type = (entry.get("sourceType") or "").strip()
    if src_type != "github":
        return None
    if "/" in src:
        author, repo = src.split("/", 1)
        author = author.strip()
        repo_full = f"{author}/{repo.strip()}"
        if author and repo:
            return (author, repo_full)
    src_url = (entry.get("sourceUrl") or "").strip()
    m = re.match(r"^https?://github\.com/([^/]+)/([^/.]+)", src_url)
    if m:
        return (m.group(1), f"{m.group(1)}/{m.group(2)}")
    return None


def load_lock() -> dict:
    if not SKILL_LOCK.is_file():
        return {}
    try:
        return json.loads(SKILL_LOCK.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def split_auto_block(text: str) -> tuple[list[str], list[str]]:
    """Return (preserved_lines, current_auto_entries)."""
    if not text:
        return [], []
    lines = text.splitlines()
    if AUTO_BLOCK_START not in text:
        return lines, []
    preserved: list[str] = []
    inside = False
    auto_entries: list[str] = []
    for ln in lines:
        if ln.strip() == AUTO_BLOCK_START:
            inside = True
            continue
        if ln.strip() == AUTO_BLOCK_END:
            inside = False
            continue
        if inside:
            stripped = ln.strip()
            if stripped and not stripped.startswith("#"):
                auto_entries.append(stripped)
        else:
            preserved.append(ln)
    return preserved, auto_entries


def write_with_auto_block(path: Path, header: str, preserved: list[str], auto_entries: list[str]) -> None:
    auto_entries = sorted(set(e for e in auto_entries if e))
    # Strip trailing blank preserved lines to avoid pile-up across runs
    while preserved and not preserved[-1].strip():
        preserved.pop()
    body = "\n".join(preserved) if preserved else header.rstrip()
    block = (
        f"\n\n{AUTO_BLOCK_START}\n"
        + "\n".join(auto_entries)
        + f"\n{AUTO_BLOCK_END}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + block, encoding="utf-8")


def sync() -> dict:
    data = load_lock()
    skills = (data.get("skills") or {})

    authors: set[str] = set()
    repos: set[str] = set()
    for name, entry in skills.items():
        ar = parse_author_repo(entry)
        if not ar:
            continue
        author, repo = ar
        # Skip auto-tune itself (it's local-sourced anyway, but guard)
        if name == "auto-tune":
            continue
        authors.add(author)
        repos.add(repo)

    # trusted_authors.txt
    prev = TRUSTED_AUTHORS.read_text(encoding="utf-8") if TRUSTED_AUTHORS.is_file() else HEADER
    preserved, _ = split_auto_block(prev)
    if not any(line.strip() and not line.startswith("#") for line in preserved):
        # First run: ensure header sits at top
        if not preserved or AUTO_BLOCK_START not in prev:
            preserved = HEADER.rstrip().splitlines()
    write_with_auto_block(TRUSTED_AUTHORS, HEADER, preserved, sorted(authors))

    # trusted_repos.txt
    repos_header = (
        "# auto-tune trusted-repos list (auto-maintained by trusted.py)\n"
        "# Exact owner/repo lines for skills the user has installed.\n"
    )
    prev_repos = TRUSTED_REPOS.read_text(encoding="utf-8") if TRUSTED_REPOS.is_file() else repos_header
    preserved_repos, _ = split_auto_block(prev_repos)
    if not any(line.strip() and not line.startswith("#") for line in preserved_repos):
        if not preserved_repos or AUTO_BLOCK_START not in prev_repos:
            preserved_repos = repos_header.rstrip().splitlines()
    write_with_auto_block(TRUSTED_REPOS, repos_header, preserved_repos, sorted(repos))

    # curators.txt — append releases.atom feed for each trusted repo (preserve user edits)
    prev_curators = CURATORS.read_text(encoding="utf-8") if CURATORS.is_file() else ""
    preserved_cur, _ = split_auto_block(prev_curators)
    if not preserved_cur:
        preserved_cur = prev_curators.splitlines() if prev_curators else []
    auto_feeds = sorted({
        f"https://github.com/{repo}/releases.atom" for repo in repos
    })
    write_with_auto_block(CURATORS, "", preserved_cur, auto_feeds)

    return {
        "authors_synced": sorted(authors),
        "repos_synced": sorted(repos),
        "curator_feeds_added": auto_feeds,
        "trusted_authors_path": str(TRUSTED_AUTHORS),
        "trusted_repos_path": str(TRUSTED_REPOS),
        "curators_path": str(CURATORS),
    }


def list_current() -> dict:
    authors: list[str] = []
    repos: list[str] = []
    if TRUSTED_AUTHORS.is_file():
        for line in TRUSTED_AUTHORS.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("# auto:"):
                authors.append(s)
    if TRUSTED_REPOS.is_file():
        for line in TRUSTED_REPOS.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("# auto:"):
                repos.append(s)
    return {"trusted_authors": sorted(set(authors)), "trusted_repos": sorted(set(repos))}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    sub.add_parser("list")
    args = p.parse_args(argv)

    if args.cmd == "sync":
        out = sync()
    elif args.cmd == "list":
        out = list_current()
    else:
        raise SystemExit(2)

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
