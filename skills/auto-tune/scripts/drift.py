#!/usr/bin/env python3
"""Drift detector: surface previously-pruned skills the user keeps asking about.

Reads `cache/log.jsonl` for recent `prune-skill` entries. For each prune, walks
transcripts since the prune timestamp and counts keyword overlap between the
pruned skill's description and the user's messages. If the user keeps
mentioning topics the pruned skill would have helped with, emit a
`restore-skill` candidate.

Output schema (cache/drift.json):
{
  "generated_at": "...",
  "candidates": [
    {
      "pruned_name": "fixing-accessibility",
      "pruned_at": "2026-03-14T...",
      "occurrence_count": 7,
      "evidence_snippets": ["please add aria labels", ...],
      "keywords_matched": ["aria", "accessibility", "keyboard"]
    }
  ],
  "diagnostics": [...]
}

propose.py reads this file and emits `restore-skill` items. Restore is
zero-cost: the source still exists at ~/.agents/skills/<name>/ (prune only
removes the symlink), so we just recreate the symlink.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

HOME = Path.home()
SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = SKILL_ROOT / "cache"
LOG_FILE = CACHE_DIR / "log.jsonl"
AGENTS_SKILLS = HOME / ".agents" / "skills"
PROJECTS_DIR = HOME / ".claude" / "projects"

# Look back 60 days; older prunes are assumed to be intentional.
LOOKBACK_DAYS = 60
# Surface as restore candidate when the user mentioned matching keywords this many times.
MIN_OCCURRENCES = 5
# Cap on snippets returned per candidate (proof-of-pattern, not exhaustive).
MAX_SNIPPETS = 5


def parse_iso(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def read_prune_events() -> list[dict]:
    """Return prune-skill log entries within LOOKBACK_DAYS."""
    if not LOG_FILE.is_file():
        return []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)
    out: list[dict] = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "prune-skill":
            continue
        ts = parse_iso(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        # id format: "prune-skill:<name>:<scope>"
        eid = entry.get("id", "")
        parts = eid.split(":")
        if len(parts) < 2:
            continue
        out.append({
            "skill_name": parts[1],
            "scope": parts[2] if len(parts) > 2 else "",
            "ts": ts,
        })
    return out


def skill_keywords(skill_name: str) -> list[str]:
    """Extract keywords from a skill's SKILL.md description.

    Looks at the source (~/.agents/skills/<name>/SKILL.md). Strips frontmatter,
    takes the description field + first 200 words of the body, returns the most
    informative tokens (drops stopwords + short words).
    """
    md = AGENTS_SKILLS / skill_name / "SKILL.md"
    if not md.is_file():
        md = AGENTS_SKILLS / skill_name / "skill" / "SKILL.md"
    if not md.is_file():
        # Last resort: derive from the skill name itself (hyphen-split).
        return [t.lower() for t in skill_name.split("-") if len(t) > 3]
    text = md.read_text(encoding="utf-8", errors="ignore")
    desc_m = re.search(r"^description:\s*(.+?)$", text, re.MULTILINE)
    desc = desc_m.group(1).strip() if desc_m else ""
    body = re.sub(r"^---.*?---", "", text, count=1, flags=re.DOTALL).strip()
    combined = (desc + " " + body[:1200]).lower()
    tokens = re.findall(r"[a-z][a-z0-9-]{3,}", combined)
    stop = {
        "this", "that", "with", "from", "your", "their", "when", "what",
        "skill", "claude", "code", "use", "uses", "used", "using", "page",
        "into", "more", "also", "such", "these", "those", "then", "than",
        "should", "would", "could", "every", "must", "very", "before", "after",
        "while", "user", "team", "they", "them", "make", "does", "doing",
    }
    counts: dict[str, int] = {}
    for tok in tokens:
        if tok in stop:
            continue
        counts[tok] = counts.get(tok, 0) + 1
    # Top 10 by frequency, but require ≥2 occurrences (one-off mentions are noise).
    top = sorted([(t, c) for t, c in counts.items() if c >= 2],
                 key=lambda x: x[1], reverse=True)[:10]
    return [t for t, _ in top] or [skill_name.split("-")[0]]


def iter_user_messages_since(since: dt.datetime, cwd: str) -> list[tuple[str, str]]:
    """Yield (session_id, user_text) for all user turns since `since`.

    Walks transcripts in PROJECTS_DIR. If `cwd` is set, restricts to the
    matching flattened-cwd dir; otherwise walks everything.
    """
    out: list[tuple[str, str]] = []
    if cwd:
        flat = "-" + re.sub(r"[/.,\s]", "-", cwd.lstrip("/"))
        roots = [PROJECTS_DIR / flat] if (PROJECTS_DIR / flat).is_dir() else list(PROJECTS_DIR.glob("*"))
    else:
        roots = list(PROJECTS_DIR.glob("*"))
    for root in roots:
        if not root.is_dir():
            continue
        for jsonl in root.glob("*.jsonl"):
            try:
                with jsonl.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = parse_iso(rec.get("timestamp"))
                        if ts is None or ts < since:
                            continue
                        msg = rec.get("message") or {}
                        if msg.get("role") != "user":
                            continue
                        content = msg.get("content")
                        if isinstance(content, str):
                            out.append((jsonl.stem, content))
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text") or ""
                                    if text:
                                        out.append((jsonl.stem, text))
            except OSError:
                continue
    return out


def count_keyword_hits(text: str, keywords: list[str]) -> tuple[int, list[str]]:
    text_lc = text.lower()
    matched: list[str] = []
    total = 0
    for kw in keywords:
        # Whole-word match to avoid "ari" matching "Saturday"
        hits = len(re.findall(rf"\b{re.escape(kw)}\b", text_lc))
        if hits > 0:
            matched.append(kw)
            total += hits
    return total, matched


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cwd", default="")
    p.add_argument("--out", default=str(CACHE_DIR / "drift.json"))
    args = p.parse_args(argv)

    prunes = read_prune_events()
    candidates: list[dict] = []
    diagnostics: list[dict] = []

    seen_skills: set[str] = set()
    for prune in prunes:
        name = prune["skill_name"]
        if name in seen_skills:
            continue
        seen_skills.add(name)
        keywords = skill_keywords(name)
        if not keywords:
            diagnostics.append({"skill": name, "_skip": "no keywords derivable"})
            continue
        msgs = iter_user_messages_since(prune["ts"], args.cwd)
        snippet_hits: list[str] = []
        kw_matches: set[str] = set()
        total_hits = 0
        for _sess, text in msgs:
            n, matched = count_keyword_hits(text, keywords)
            if n > 0:
                total_hits += n
                kw_matches.update(matched)
                if len(snippet_hits) < MAX_SNIPPETS:
                    snippet_hits.append(text.strip()[:180])

        if total_hits < MIN_OCCURRENCES:
            diagnostics.append({
                "skill": name,
                "_skip": f"only {total_hits} keyword hits since prune ({prune['ts'].isoformat()}), need {MIN_OCCURRENCES}",
                "keywords": keywords,
            })
            continue

        candidates.append({
            "pruned_name": name,
            "pruned_at": prune["ts"].isoformat(),
            "occurrence_count": total_hits,
            "keywords_matched": sorted(kw_matches),
            "evidence_snippets": snippet_hits,
            "source_exists": (AGENTS_SKILLS / name / "SKILL.md").is_file() or (AGENTS_SKILLS / name / "skill" / "SKILL.md").is_file(),
        })

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "min_occurrences": MIN_OCCURRENCES,
        "candidates": candidates,
        "diagnostics": diagnostics,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    summary = {
        "generated_at": out["generated_at"],
        "candidate_count": len(candidates),
        "diagnostic_count": len(diagnostics),
        "candidates_preview": [c["pruned_name"] for c in candidates],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
