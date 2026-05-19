#!/usr/bin/env python3
"""Detect user-correction patterns in transcripts.

Three signals, all batch, all read-only:

1. negation         assistant turn → user turn starting with "no", "don't",
                    "stop", "actually", "that's wrong", "not (that|like)", "undo".
2. rework_cycle     same first-message intent cluster recurs >=3 times within
                    one session (Jaccard >= 0.45 over normalized tokens).
3. command_misfire  Skill tool invocation followed within 2 user turns by a
                    negation OR an Edit that undoes a same-skill Edit.

The implicated skill is the most-recent Skill invocation in the same session
prior to the negation (search backwards up to 20 turns). If no Skill was
invoked, the pattern is attributed to "(global)" so it can drive a
CLAUDE.md-level tweak instead.

Output: cache/corrections.json with raw pattern data only. The orchestrator
(SKILL.md) is expected to draft proposed_edit text per candidate before
propose.py consumes the file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
SKILL_ROOT = Path(__file__).resolve().parent.parent

WINDOW_DAYS = 60
CUTOFF = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW_DAYS)

NEGATION_PATTERNS = [
    re.compile(r"\b(don'?t|do not)\b", re.IGNORECASE),
    re.compile(r"\bnot (that|the|in|like|quite|exactly|what|right|correct)\b", re.IGNORECASE),
    re.compile(r"\b(undo|revert|rollback)\b", re.IGNORECASE),
    re.compile(r"\bthat'?s (wrong|not (right|what))\b", re.IGNORECASE),
    re.compile(r"^\s*(stop|wait,? no|nope|no,)\b", re.IGNORECASE),
    re.compile(r"\b(actually|hold on|wait,?)\s+(no|i|we|that|the)\b", re.IGNORECASE),
    re.compile(r"\bi (still )?don'?t (see|want|need|like)\b", re.IGNORECASE),
]
FALSE_POSITIVE_PATTERNS = [
    re.compile(r"\bno problem\b", re.IGNORECASE),
    re.compile(r"\bno worries\b", re.IGNORECASE),
    re.compile(r"\bnot only\b", re.IGNORECASE),
    re.compile(r"\bnot just\b", re.IGNORECASE),
]
EXPLICIT_SKILL_REVERT = re.compile(r"\b(don'?t use|stop using|wrong skill|not (the )?\w+ skill)\b", re.IGNORECASE)


def looks_like_correction(text: str) -> bool:
    if any(fp.search(text) for fp in FALSE_POSITIVE_PATTERNS):
        if not any(re.search(r"\b(don'?t|undo|revert|wrong|actually|stop)\b", text, re.IGNORECASE)
                   for _ in [None]):
            return False
    return any(p.search(text) for p in NEGATION_PATTERNS)

GRADUATION_THRESHOLD = 3


def parse_ts(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_intent(text: str) -> set[str]:
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
    return len(a & b) / max(1, len(a | b))


def load_turns(path: Path) -> list[dict]:
    turns: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return turns


def first_text(content) -> str:
    if not isinstance(content, list):
        return ""
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            return (b.get("text") or "").strip()
    return ""


def assistant_text(rec: dict) -> str:
    msg = rec.get("message") or {}
    if msg.get("role") != "assistant":
        return ""
    return first_text(msg.get("content"))


def user_text(rec: dict) -> str:
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return ""
    return first_text(msg.get("content"))


def skill_invocations(rec: dict) -> list[str]:
    out: list[str] = []
    msg = rec.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return out
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        if b.get("name") == "Skill":
            sk = (b.get("input") or {}).get("skill")
            if sk:
                out.append(sk)
    return out


def detect_in_session(turns: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    negations: list[dict] = []
    misfires: list[dict] = []
    rework: list[dict] = []

    intents: list[set[str]] = []
    last_assistant_idx: int | None = None
    last_skill: tuple[str, int] | None = None

    for i, rec in enumerate(turns):
        ts = parse_ts(rec.get("timestamp"))
        if ts and ts < CUTOFF:
            continue

        skills = skill_invocations(rec)
        for sk in skills:
            last_skill = (sk, i)

        if assistant_text(rec):
            last_assistant_idx = i

        ut = user_text(rec)
        if ut:
            tokens = normalize_intent(ut)
            if tokens:
                intents.append(tokens)
            if looks_like_correction(ut) and last_assistant_idx is not None:
                attributed_skill = "(global)"
                if last_skill and (i - last_skill[1]) <= 20:
                    attributed_skill = last_skill[0]
                snippet = ut[:240]
                negations.append({
                    "skill": attributed_skill,
                    "snippet": snippet,
                    "session_id": rec.get("sessionId"),
                    "turn_index": i,
                })
                if last_skill and (i - last_skill[1]) <= 4:
                    misfires.append({
                        "skill": last_skill[0],
                        "snippet": snippet,
                        "session_id": rec.get("sessionId"),
                        "turn_index": i,
                    })
                elif EXPLICIT_SKILL_REVERT.search(ut) and last_skill:
                    misfires.append({
                        "skill": last_skill[0],
                        "snippet": snippet,
                        "session_id": rec.get("sessionId"),
                        "turn_index": i,
                    })

    if len(intents) >= 3:
        cluster_sim = 0
        base = intents[0]
        for other in intents[1:]:
            if jaccard(base, other) >= 0.45:
                cluster_sim += 1
        if cluster_sim >= 2:
            rework.append({
                "skill": (last_skill[0] if last_skill else "(global)"),
                "snippet": "session repeated the same intent cluster",
                "session_id": turns[0].get("sessionId") if turns else None,
                "intent_count": cluster_sim + 1,
            })

    return negations, misfires, rework


def graduate(events: list[dict], kind: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        grouped[ev["skill"]].append(ev)
    out: list[dict] = []
    for skill, items in grouped.items():
        if len(items) < GRADUATION_THRESHOLD:
            continue
        snippets = [it.get("snippet", "") for it in items[:5]]
        sessions = sorted({it.get("session_id") for it in items if it.get("session_id")})[:5]
        out.append({
            "kind": kind,
            "skill": skill,
            "count": len(items),
            "sample_snippets": snippets,
            "session_ids": sessions,
            "proposed_edit": None,
        })
    return sorted(out, key=lambda c: -c["count"])


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(SKILL_ROOT / "cache" / "corrections.json"))
    p.add_argument("--project", help="restrict to one flattened project dir name")
    args = p.parse_args(argv)

    if not PROJECTS_DIR.is_dir():
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"candidates": [], "diagnostics": ["no projects dir"]}, indent=2))
        print(json.dumps({"wrote": args.out, "candidates": 0}, indent=2))
        return 0

    project_dirs = (
        [PROJECTS_DIR / args.project] if args.project else sorted(PROJECTS_DIR.glob("*/"))
    )

    all_neg: list[dict] = []
    all_mis: list[dict] = []
    all_rew: list[dict] = []
    sessions_scanned = 0
    for proj in project_dirs:
        if not proj.is_dir():
            continue
        for jsonl in sorted(proj.glob("*.jsonl")):
            turns = load_turns(jsonl)
            if not turns:
                continue
            sessions_scanned += 1
            n, m, r = detect_in_session(turns)
            all_neg += n
            all_mis += m
            all_rew += r

    candidates = (
        graduate(all_neg, "negation")
        + graduate(all_mis, "command_misfire")
        + graduate(all_rew, "rework_cycle")
    )

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "sessions_scanned": sessions_scanned,
        "raw_event_counts": {
            "negation": len(all_neg),
            "command_misfire": len(all_mis),
            "rework_cycle": len(all_rew),
        },
        "candidates": candidates,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": args.out,
        "sessions_scanned": sessions_scanned,
        "raw_event_counts": out["raw_event_counts"],
        "candidates": len(candidates),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
