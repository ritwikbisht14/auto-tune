#!/usr/bin/env python3
"""Parse Claude Code transcripts and emit a signals.json bundle.

Default scope: only transcripts whose `cwd` field matches the target project.
With --global: scan every project directory under ~/.claude/projects/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

WINDOW_DAYS = 90
NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(days=WINDOW_DAYS)


def flatten_cwd(cwd: str) -> str:
    p = cwd.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    return "-" + re.sub(r"[/.,\s]", "-", p)


def tool_prefix(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 2:
            return "__".join(parts[:2])
    return name


def extension_of(path: str) -> str:
    base = os.path.basename(path)
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def parse_timestamp(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def first_user_text(records: list[dict]) -> str:
    for rec in records:
        if rec.get("type") != "user":
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if text.strip():
                        return text.strip()
    return ""


def normalize_intent(text: str) -> set[str]:
    """Token set for cluster comparison."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    stop = {
        "the", "a", "an", "and", "or", "but", "to", "of", "for", "in", "on", "at",
        "is", "are", "be", "this", "that", "with", "as", "i", "you", "we", "my",
        "can", "could", "would", "please", "help", "me", "do", "it", "if", "so",
        "from", "by", "any", "some", "have", "has", "had", "let", "lets",
    }
    return {w for w in text.split() if len(w) > 2 and w not in stop}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def cluster_intents(intents: list[tuple[str, set[str]]], threshold: float = 0.45) -> list[dict]:
    """Greedy clustering on Jaccard similarity over normalized token sets."""
    clusters: list[dict] = []
    for raw, tokens in intents:
        placed = False
        for c in clusters:
            if jaccard(tokens, c["tokens"]) >= threshold:
                c["members"].append(raw)
                c["count"] += 1
                c["tokens"] = c["tokens"] | tokens
                placed = True
                break
        if not placed:
            clusters.append({"tokens": set(tokens), "members": [raw], "count": 1})
    for c in clusters:
        c["tokens"] = sorted(c["tokens"])
    return sorted(clusters, key=lambda c: -c["count"])


def analyze_session(jsonl_path: Path) -> dict | None:
    records: list[dict] = []
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    if not records:
        return None

    cwd = None
    last_ts: dt.datetime | None = None
    tool_calls: Counter[str] = Counter()
    raw_tool_calls: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    skill_invocations: Counter[str] = Counter()
    agent_invocations: Counter[str] = Counter()
    input_tokens = 0
    cache_read = 0
    cache_create = 0
    user_message_count = 0

    for rec in records:
        if cwd is None:
            cwd = rec.get("cwd")
        ts = parse_timestamp(rec.get("timestamp"))
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts

        msg = rec.get("message") or {}
        usage = msg.get("usage") or {}
        input_tokens += usage.get("input_tokens", 0) or 0
        cache_read += usage.get("cache_read_input_tokens", 0) or 0
        cache_create += usage.get("cache_creation_input_tokens", 0) or 0

        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name", "")
                raw_tool_calls[name] += 1
                tool_calls[tool_prefix(name)] += 1
                if name == "Skill":
                    inp = block.get("input") or {}
                    skill_name = inp.get("skill")
                    if skill_name:
                        skill_invocations[skill_name] += 1
                if name == "Agent":
                    inp = block.get("input") or {}
                    subagent_type = inp.get("subagent_type")
                    if subagent_type:
                        agent_invocations[subagent_type] += 1
                inp = block.get("input") or {}
                for key in ("file_path", "path", "notebook_path"):
                    v = inp.get(key)
                    if isinstance(v, str):
                        ext = extension_of(v)
                        if ext:
                            extensions[ext] += 1
            elif btype == "text" and msg.get("role") == "user":
                user_message_count += 1

    if cwd is None:
        return None

    intent_raw = first_user_text(records)
    intent_tokens = list(normalize_intent(intent_raw)) if intent_raw else []

    return {
        "session_id": jsonl_path.stem,
        "cwd": cwd,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "tool_calls": dict(tool_calls),
        "raw_tool_calls": dict(raw_tool_calls),
        "extensions": dict(extensions),
        "skill_invocations": dict(skill_invocations),
        "agent_invocations": dict(agent_invocations),
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": cache_create,
        "user_message_count": user_message_count,
        "first_user_intent": intent_raw[:280],
        "intent_tokens": intent_tokens,
    }


def iter_jsonl_files(scope: str, cwd: str) -> list[Path]:
    if not PROJECTS_DIR.is_dir():
        return []
    if scope == "global":
        return sorted(PROJECTS_DIR.glob("*/*.jsonl"))
    flattened = flatten_cwd(cwd)
    proj_dir = PROJECTS_DIR / flattened
    if not proj_dir.is_dir():
        return []
    return sorted(proj_dir.glob("*.jsonl"))


def aggregate(sessions: list[dict]) -> dict:
    by_project: dict[str, dict] = defaultdict(lambda: {
        "session_count": 0,
        "tool_calls": Counter(),
        "raw_tool_calls": Counter(),
        "extensions": Counter(),
        "skill_invocations": Counter(),
        "agent_invocations": Counter(),
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "last_ts": None,
        "intents": [],
    })

    in_window: dict[str, dict] = defaultdict(lambda: {
        "tool_calls": Counter(),
        "skill_invocations": Counter(),
        "agent_invocations": Counter(),
    })

    for s in sessions:
        proj = s["cwd"] or "(unknown)"
        bucket = by_project[proj]
        bucket["session_count"] += 1
        bucket["tool_calls"].update(s["tool_calls"])
        bucket["raw_tool_calls"].update(s["raw_tool_calls"])
        bucket["extensions"].update(s["extensions"])
        bucket["skill_invocations"].update(s["skill_invocations"])
        bucket["agent_invocations"].update(s.get("agent_invocations", {}))
        bucket["input_tokens"] += s["input_tokens"]
        bucket["cache_read_tokens"] += s["cache_read_tokens"]
        bucket["cache_create_tokens"] += s["cache_create_tokens"]
        if s["last_ts"]:
            if not bucket["last_ts"] or s["last_ts"] > bucket["last_ts"]:
                bucket["last_ts"] = s["last_ts"]
        if s["first_user_intent"]:
            bucket["intents"].append((s["first_user_intent"], set(s["intent_tokens"])))

        ts = parse_timestamp(s["last_ts"]) if s["last_ts"] else None
        if ts and ts >= CUTOFF:
            in_window[proj]["tool_calls"].update(s["tool_calls"])
            in_window[proj]["skill_invocations"].update(s["skill_invocations"])
            in_window[proj]["agent_invocations"].update(s.get("agent_invocations", {}))

    out_projects = {}
    for proj, b in by_project.items():
        clusters = cluster_intents(b["intents"])
        out_projects[proj] = {
            "session_count": b["session_count"],
            "tool_calls": dict(b["tool_calls"]),
            "raw_tool_calls": dict(b["raw_tool_calls"]),
            "extensions": dict(b["extensions"]),
            "skill_invocations": dict(b["skill_invocations"]),
            "agent_invocations": dict(b["agent_invocations"]),
            "input_tokens": b["input_tokens"],
            "cache_read_tokens": b["cache_read_tokens"],
            "cache_create_tokens": b["cache_create_tokens"],
            "last_ts": b["last_ts"],
            "in_window_tool_calls": dict(in_window[proj]["tool_calls"]),
            "in_window_skill_invocations": dict(in_window[proj]["skill_invocations"]),
            "in_window_agent_invocations": dict(in_window[proj]["agent_invocations"]),
            "intent_clusters": [
                {"count": c["count"], "members": c["members"][:5], "tokens": c["tokens"][:20]}
                for c in clusters if c["count"] >= 2
            ][:10],
        }
    return out_projects


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cwd", required=True)
    p.add_argument("--global", dest="global_scope", action="store_true")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    scope = "global" if args.global_scope else "project"
    jsonl_files = iter_jsonl_files(scope, args.cwd)

    sessions = []
    for path in jsonl_files:
        s = analyze_session(path)
        if s:
            sessions.append(s)

    enabled_skills = []
    skills_dir = CLAUDE_DIR / "skills"
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_symlink() or entry.is_dir():
                enabled_skills.append(entry.name)

    mcp_servers = []
    claude_json = HOME / ".claude.json"
    if claude_json.is_file():
        try:
            data = json.loads(claude_json.read_text(encoding="utf-8", errors="ignore"))
            mcp_servers = sorted((data.get("mcpServers") or {}).keys())
        except json.JSONDecodeError:
            pass

    out = {
        "generated_at": NOW.isoformat(),
        "scope": scope,
        "cwd": args.cwd,
        "window_days": WINDOW_DAYS,
        "session_count": len(sessions),
        "enabled_skills": enabled_skills,
        "mcp_servers": mcp_servers,
        "projects": aggregate(sessions),
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": args.out,
        "sessions": len(sessions),
        "projects": len(out["projects"]),
        "enabled_skills": len(enabled_skills),
        "mcp_servers": len(mcp_servers),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
