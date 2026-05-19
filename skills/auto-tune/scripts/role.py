#!/usr/bin/env python3
"""Role detection and storage for auto-tune.

Priority order:
1. --override
2. <cwd>/.claude/.role
3. ~/.claude/.role
4. Memory files under ~/.claude/projects/<flattened>/memory/ (user_*.md or feedback_*.md)
5. Transcript heuristic (file extensions + tool/MCP usage + user-message keywords)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROLES = ("designer", "pm", "engineer")
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

ROLE_KEYWORDS = {
    "designer": {
        "tools": {"mcp__figma-dev", "mcp__chrome-devtools"},
        "extensions": {".tsx", ".jsx", ".css", ".scss", ".fig"},
        "keywords": {"design", "figma", "spec", "prototype", "ui", "ux", "mock", "wireframe", "screenshot"},
    },
    "pm": {
        "tools": {"mcp__claude_ai_Atlassian_Rovo", "mcp__claude_ai_Microsoft_365", "mcp__claude_ai_Slack"},
        "extensions": {".md"},
        "keywords": {"prd", "ticket", "jira", "linear", "requirement", "stakeholder", "roadmap", "epic", "story"},
    },
    "engineer": {
        "tools": {"Bash", "Edit", "Write"},
        "extensions": {".ts", ".py", ".go", ".rs", ".java", ".test.ts", ".spec.ts", ".test.py"},
        "keywords": {"implement", "refactor", "fix", "bug", "test", "ci", "build", "deploy", "compile"},
    },
}


def flatten_cwd(cwd: str) -> str:
    """Match Claude Code's transcript directory naming: leading dash, slashes -> dashes, dots -> dashes."""
    p = cwd.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    return "-" + re.sub(r"[/.,\s]", "-", p)


def read_role_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    val = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    return val if val in ROLES else None


def read_memory_signals(project_dir: Path) -> list[tuple[str, str]]:
    """Return [(role, evidence_path)] from memory files that explicitly name a role."""
    out: list[tuple[str, str]] = []
    memory_dir = project_dir / "memory"
    if not memory_dir.is_dir():
        return out
    for md in memory_dir.glob("*.md"):
        if md.name == "MEMORY.md":
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for role in ROLES:
            phrases = {
                "designer": ("designer", "design lead", "ux lead"),
                "pm": ("product manager", " pm ", "program manager"),
                "engineer": ("software engineer", "swe ", " developer", "backend engineer", "frontend engineer"),
            }[role]
            if any(p in text for p in phrases):
                out.append((role, str(md)))
                break
    return out


def transcript_heuristic(project_dir: Path) -> tuple[str, float, list[str]]:
    """Score role from transcripts; return (role, confidence, evidence)."""
    if not project_dir.is_dir():
        return ("engineer", 0.0, ["no transcripts"])

    scores: Counter[str] = Counter()
    evidence: list[str] = []
    ext_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()
    line_count = 0

    for jsonl in project_dir.glob("*.jsonl"):
        try:
            with jsonl.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line_count += 1
                    if line_count > 50000:
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = rec.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            btype = block.get("type") if isinstance(block, dict) else None
                            if btype == "tool_use":
                                name = block.get("name", "")
                                tool_counts[_tool_prefix(name)] += 1
                                inp = block.get("input") or {}
                                for key in ("file_path", "path", "notebook_path"):
                                    v = inp.get(key)
                                    if isinstance(v, str):
                                        ext = _extension(v)
                                        if ext:
                                            ext_counts[ext] += 1
                            elif btype == "text" and msg.get("role") == "user":
                                text = (block.get("text") or "").lower()
                                for role, prof in ROLE_KEYWORDS.items():
                                    for kw in prof["keywords"]:
                                        if kw in text:
                                            keyword_counts[f"{role}:{kw}"] += 1
        except OSError:
            continue

    for role, prof in ROLE_KEYWORDS.items():
        tool_score = sum(tool_counts.get(t, 0) for t in prof["tools"])
        ext_score = sum(ext_counts.get(e, 0) for e in prof["extensions"])
        kw_score = sum(c for k, c in keyword_counts.items() if k.startswith(f"{role}:"))
        scores[role] = tool_score * 2 + ext_score + kw_score
        if tool_score:
            evidence.append(f"{role}: {tool_score} role-tool calls")
        if ext_score:
            evidence.append(f"{role}: {ext_score} role-extension edits")
        if kw_score:
            evidence.append(f"{role}: {kw_score} role-keyword hits")

    if not scores or sum(scores.values()) == 0:
        return ("engineer", 0.0, ["no signal; defaulting to engineer"])

    role, top = scores.most_common(1)[0]
    total = sum(scores.values())
    confidence = top / total if total else 0.0
    return (role, confidence, evidence)


def _tool_prefix(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 2:
            return "__".join(parts[:2])
    return name


def _extension(path: str) -> str:
    base = os.path.basename(path)
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def detect(cwd: str, override: str | None) -> dict:
    if override:
        if override not in ROLES:
            raise SystemExit(f"unknown role: {override}")
        return {"role": override, "source": "override", "confidence": 1.0, "evidence": []}

    cwd_path = Path(cwd).expanduser().resolve()
    project_role_file = cwd_path / ".claude" / ".role"
    global_role_file = CLAUDE_DIR / ".role"

    project_role = read_role_file(project_role_file)
    if project_role:
        return {"role": project_role, "source": "file:project", "confidence": 1.0, "evidence": [str(project_role_file)]}

    global_role = read_role_file(global_role_file)

    project_dir = PROJECTS_DIR / flatten_cwd(str(cwd_path))
    mem = read_memory_signals(project_dir)
    if mem:
        role = Counter(r for r, _ in mem).most_common(1)[0][0]
        return {
            "role": role,
            "source": "memory",
            "confidence": 0.9,
            "evidence": [p for _, p in mem],
        }

    if global_role:
        return {"role": global_role, "source": "file:global", "confidence": 0.8, "evidence": [str(global_role_file)]}

    role, conf, ev = transcript_heuristic(project_dir)
    return {"role": role, "source": "heuristic", "confidence": round(conf, 3), "evidence": ev}


def set_role(scope: str, role: str, cwd: str) -> dict:
    if role not in ROLES:
        raise SystemExit(f"unknown role: {role}")
    if scope == "global":
        target = CLAUDE_DIR / ".role"
    elif scope == "project":
        target = Path(cwd).expanduser().resolve() / ".claude" / ".role"
    else:
        raise SystemExit(f"unknown scope: {scope}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(role + "\n", encoding="utf-8")
    return {"written": str(target), "role": role, "scope": scope}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect")
    d.add_argument("--cwd", required=True)
    d.add_argument("--override")

    s = sub.add_parser("set")
    s.add_argument("--scope", required=True, choices=["global", "project"])
    s.add_argument("--role", required=True)
    s.add_argument("--cwd", required=True)

    args = p.parse_args(argv)

    if args.cmd == "detect":
        out = detect(args.cwd, args.override)
    elif args.cmd == "set":
        out = set_role(args.scope, args.role, args.cwd)
    else:
        raise SystemExit(2)

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
