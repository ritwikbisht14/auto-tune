#!/usr/bin/env python3
"""Security gate for auto-tune internet fetches.

Three commands:
  check-url <url>           -> {allowed, host, matched_rule, reason}
  scan-content <path>       -> {file, status: clean|flagged, findings, sha256}
  quarantine-fetch <url>    -> downloads to security/quarantine/<sha256>/ (after check-url)
                               then runs scan-content on it; returns the merged result.

Hard rules:
- Refuses to fetch anything off the allowlist.
- Never auto-promotes from quarantine; that's apply.py's job after explicit approval.
- Exits non-zero when an action is refused so callers (discover.py, apply.py) can branch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SECURITY_DIR = SKILL_ROOT / "security"
ALLOWLIST_PATH = SECURITY_DIR / "allowlist.txt"
PATTERNS_PATH = SECURITY_DIR / "patterns.json"
QUARANTINE_DIR = SECURITY_DIR / "quarantine"

FETCH_TIMEOUT = 15
MAX_BODY_BYTES = 4 * 1024 * 1024  # 4MB cap per file


def load_allowlist() -> list[str]:
    rules: list[str] = []
    if not ALLOWLIST_PATH.is_file():
        return rules
    for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(line.lower())
    return rules


def host_matches(host: str, rule: str) -> bool:
    host = host.lower()
    rule = rule.lower()
    if rule.startswith("*."):
        suffix = rule[1:]  # ".github.com"
        return host == suffix.lstrip(".") or host.endswith(suffix)
    return host == rule


def check_url(url: str) -> dict:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as e:
        return {"allowed": False, "url": url, "reason": f"unparseable: {e}"}

    if parsed.scheme not in ("http", "https"):
        return {"allowed": False, "url": url, "reason": f"scheme not http(s): {parsed.scheme!r}"}

    host = parsed.hostname or ""
    if not host:
        return {"allowed": False, "url": url, "reason": "missing host"}

    if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", host):
        return {"allowed": False, "url": url, "host": host, "reason": "raw IP literal blocked"}

    try:
        if socket.gethostbyname(host).startswith("127."):
            return {"allowed": False, "url": url, "host": host, "reason": "loopback target"}
    except OSError:
        pass

    rules = load_allowlist()
    for rule in rules:
        if host_matches(host, rule):
            return {"allowed": True, "url": url, "host": host, "matched_rule": rule}

    return {"allowed": False, "url": url, "host": host, "reason": "host-not-in-allowlist"}


def load_patterns() -> list[dict]:
    if not PATTERNS_PATH.is_file():
        return []
    try:
        data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data.get("patterns", [])


def scan_content(path: Path) -> dict:
    if not path.is_file():
        return {"file": str(path), "status": "error", "reason": "not a file"}
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"file": str(path), "status": "error", "reason": str(e)}

    sha = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
    findings: list[dict] = []
    for p in load_patterns():
        try:
            matches = list(re.finditer(p["regex"], body))
        except re.error as e:
            findings.append({"pattern": p["name"], "error": f"bad regex: {e}"})
            continue
        if matches:
            sample = matches[0].group(0)
            findings.append({
                "pattern": p["name"],
                "severity": p.get("severity", "medium"),
                "count": len(matches),
                "sample": sample[:160],
            })

    status = "flagged" if any(f.get("severity") in ("high", "medium") for f in findings) else "clean"
    return {
        "file": str(path),
        "status": status,
        "sha256": sha,
        "bytes": len(body),
        "findings": findings,
    }


def quarantine_fetch(url: str) -> dict:
    gate = check_url(url)
    if not gate["allowed"]:
        return {"status": "refused", "stage": "check-url", **gate}

    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "auto-tune-discover/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.URLError as e:
        return {"status": "error", "stage": "fetch", "url": url, "reason": str(e)}
    except socket.timeout:
        return {"status": "error", "stage": "fetch", "url": url, "reason": "timeout"}

    if len(body) > MAX_BODY_BYTES:
        return {"status": "refused", "stage": "fetch", "url": url, "reason": "body exceeds 4MB cap"}

    sha = hashlib.sha256(body).hexdigest()
    dest_dir = QUARANTINE_DIR / sha
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / "content"
    dest_file.write_bytes(body)
    (dest_dir / "meta.json").write_text(
        json.dumps({"url": url, "content_type": content_type, "bytes": len(body)}, indent=2),
        encoding="utf-8",
    )

    scan = scan_content(dest_file)
    return {
        "status": scan["status"],
        "stage": "scan",
        "url": url,
        "quarantine_path": str(dest_file),
        "sha256": sha,
        "findings": scan["findings"],
        "bytes": len(body),
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    cu = sub.add_parser("check-url")
    cu.add_argument("url")

    sc = sub.add_parser("scan-content")
    sc.add_argument("path")

    qf = sub.add_parser("quarantine-fetch")
    qf.add_argument("url")

    args = p.parse_args(argv)

    if args.cmd == "check-url":
        out = check_url(args.url)
        rc = 0 if out["allowed"] else 1
    elif args.cmd == "scan-content":
        out = scan_content(Path(args.path))
        rc = 0 if out.get("status") == "clean" else 1
    elif args.cmd == "quarantine-fetch":
        out = quarantine_fetch(args.url)
        rc = 0 if out.get("status") == "clean" else 1
    else:
        raise SystemExit(2)

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
