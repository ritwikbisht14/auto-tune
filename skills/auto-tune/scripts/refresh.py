#!/usr/bin/env python3
"""Upgrade detector: find better community alternatives to installed skills.

For each facet in the user's role where the primary skill is installed, look
for high-quality candidates in `cache/candidates.json` (produced by discover.py)
that match the facet's keywords. If a candidate scores above the upgrade
threshold and isn't the same skill, emit a `swap-skill` entry to
`cache/upgrades.json`.

Output schema (cache/upgrades.json):
{
  "generated_at": "...",
  "role": "designer",
  "upgrades": [
    {
      "facet": "accessibility",
      "installed": "fixing-accessibility",
      "candidate": {"name": "...", "source_url": "...", "quality_score": 0.78, ...},
      "score_delta": 0.18,
      "rationale": "candidate scored 0.78 vs assumed-baseline 0.60 for installed"
    },
    ...
  ],
  "diagnostics": [...]
}

propose.py reads this file and emits `swap-skill` items.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = SKILL_ROOT / "cache"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import compose  # noqa: E402  reuses FACETS + candidate_matches_keywords + list_enabled_skills

# Quality threshold for surfacing as upgrade candidate (0.0 - 1.0).
# Chosen empirically: 0.7 = community skill must be clearly better than a
# generic installed default to warrant a swap.
UPGRADE_THRESHOLD = 0.7

# Assumed baseline quality_score for an installed skill that the user manually
# chose. Higher than 0.5 because manual install is itself a signal of quality.
INSTALLED_BASELINE = 0.60

# Minimum delta over installed baseline to surface (avoids tiny noise wins).
MIN_DELTA = 0.15


def load_candidates() -> list[dict]:
    p = CACHE_DIR / "candidates.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data.get("candidates", []) if isinstance(data, dict) else data


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--out", default=str(CACHE_DIR / "upgrades.json"))
    args = p.parse_args(argv)

    facets = compose.FACETS.get(args.role, {})
    if not facets:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps({"role": args.role, "upgrades": [], "note": "no facets defined for role"}, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"role": args.role, "upgrades": 0, "note": "no facets"}, indent=2))
        return 0

    enabled = set(compose.list_enabled_skills())
    candidates = load_candidates()

    upgrades: list[dict] = []
    diagnostics: list[dict] = []

    for facet_name, facet_def in facets.items():
        primary = facet_def.get("primary")
        if not primary or primary not in enabled:
            diagnostics.append({"facet": facet_name, "_skip": "facet primary not installed; nothing to upgrade"})
            continue

        keywords = facet_def.get("keywords", [])
        matching = []
        for cand in candidates:
            if compose.candidate_matches_keywords(cand, keywords) >= 1:
                matching.append(cand)

        matching.sort(key=lambda c: c.get("quality_score", 0.0), reverse=True)
        top = matching[0] if matching else None

        if not top:
            diagnostics.append({"facet": facet_name, "_skip": "no candidates matched facet keywords"})
            continue

        cand_score = float(top.get("quality_score", 0.0))
        cand_name = top.get("name", "")
        cand_repo = (top.get("source_url") or "").rstrip("/").split("/")[-1]

        if cand_name == primary or cand_repo == primary:
            diagnostics.append({"facet": facet_name, "_skip": f"top candidate is same as installed ({primary})"})
            continue

        delta = cand_score - INSTALLED_BASELINE
        if cand_score < UPGRADE_THRESHOLD or delta < MIN_DELTA:
            diagnostics.append({
                "facet": facet_name,
                "_skip": f"top candidate {cand_name} scored {cand_score:.2f} < threshold {UPGRADE_THRESHOLD} or delta {delta:.2f} < {MIN_DELTA}",
            })
            continue

        # v5.4 triple-gate: only emit swap-skill when the candidate is verifiably
        # a real skill (has SKILL.md) AND genuinely role-relevant (not just
        # trust-boosted) AND clears the score delta.
        insp = top.get("inspection") or {}
        if not insp.get("skill_md_path"):
            diagnostics.append({
                "facet": facet_name,
                "_skip": f"top candidate {cand_name} has no SKILL.md (inspection.skill_md_path=None) — refusing swap",
                "candidate_url": top.get("source_url") or top.get("html_url"),
            })
            continue
        if float(top.get("role_relevance", 0.0)) < 0.5:
            diagnostics.append({
                "facet": facet_name,
                "_skip": f"top candidate {cand_name} role_relevance {top.get('role_relevance', 0):.2f} < 0.5 — likely trust-boosted, not genuinely role-fit",
                "candidate_url": top.get("source_url") or top.get("html_url"),
            })
            continue

        upgrades.append({
            "facet": facet_name,
            "installed": primary,
            "candidate": {
                "name": cand_name,
                "source_url": top.get("source_url"),
                "description": top.get("description", "")[:200],
                "quality_score": cand_score,
                "popularity": top.get("popularity"),
                "last_activity_at": top.get("last_activity_at"),
            },
            "score_delta": round(delta, 3),
            "rationale": f"candidate scored {cand_score:.2f} vs assumed installed baseline {INSTALLED_BASELINE:.2f}",
        })

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "role": args.role,
        "cwd": args.cwd,
        "upgrade_threshold": UPGRADE_THRESHOLD,
        "installed_baseline": INSTALLED_BASELINE,
        "min_delta": MIN_DELTA,
        "upgrades": upgrades,
        "diagnostics": diagnostics,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    summary = {k: v for k, v in out.items() if k not in ("upgrades", "diagnostics")}
    summary["upgrade_count"] = len(upgrades)
    summary["diagnostic_count"] = len(diagnostics)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
