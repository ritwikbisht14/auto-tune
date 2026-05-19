#!/usr/bin/env python3
"""Opinionated skill-set composer for auto-tune.

Picks a coherent bundle of skills covering orthogonal facets of a role's
workflow (e.g. designer: research / spec / implementation / a11y / motion /
polish / metadata), drawn from:
  - skills already installed in ~/.claude/skills/
  - clean discovery candidates from cache/candidates.json
  - a small built-in opinion map (FACETS below)

Also generates per-skill personalization context derived from this user's
transcripts, memory entries, and recorded corrections — so a community
skill behaves like it was written for this project.

Output: cache/composition.json with
  - facets: [{name, status, picked_skill, source, rationale}]
  - bundle_actions: [{skill, action: keep|install|disable_in_folder}]
  - token_budget: {target, before_bytes, after_bytes, delta_bytes, est_tokens_saved}
  - personalizations: [{skill, target_path, context_block, evidence}]

Heavy lifting only. propose.py consumes the file and emits proposal items.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path
import sys

HOME = Path.home()
SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = SKILL_ROOT / "cache"
AGENTS_SKILLS = HOME / ".agents" / "skills"
CLAUDE_SKILLS = HOME / ".claude" / "skills"
PROJECTS_DIR = HOME / ".claude" / "projects"

DEFAULT_TARGET_TOKENS = 2000
BYTES_PER_TOKEN = 4


# Opinionated facet map.  Each facet lists primary + fallback skills in
# preference order.  "primary" is the editor-curated pick; "fallbacks" are
# acceptable substitutes.  None means: search candidates by keyword.
FACETS = {
    "designer": {
        "research": {
            "primary": "claude-studio-design-partner-skill",
            "fallbacks": [],
            "keywords": ["research", "competitor", "inspiration", "design studio"],
            "blurb": "Surveys references, captures inspiration, structures research notes.",
        },
        "spec_audit": {
            "primary": "impeccable",
            "fallbacks": ["emil-design-eng"],
            "keywords": ["audit", "critique", "design system", "polish"],
            "blurb": "Audits and critiques UI; turns rough sketches into specs.",
        },
        "implementation": {
            "primary": "baseline-ui",
            "fallbacks": ["vercel-react-best-practices"],
            "keywords": ["tailwind", "design tokens", "components", "ui"],
            "blurb": "Enforces design tokens, typography scale, component patterns.",
        },
        "accessibility": {
            "primary": "fixing-accessibility",
            "fallbacks": [],
            "keywords": ["accessibility", "a11y", "aria", "wcag"],
            "blurb": "ARIA, keyboard nav, focus, contrast, form errors.",
        },
        "motion": {
            "primary": "fixing-motion-performance",
            "fallbacks": [],
            "keywords": ["motion", "animation", "transitions"],
            "blurb": "Animation performance, compositor properties, scroll-linked motion.",
        },
        "polish": {
            "primary": "emil-design-eng",
            "fallbacks": ["impeccable"],
            "keywords": ["polish", "invisible details", "ui polish"],
            "blurb": "The invisible details that make software feel great.",
        },
        "metadata": {
            "primary": "fixing-metadata",
            "fallbacks": [],
            "keywords": ["metadata", "seo", "og tags", "twitter cards"],
            "blurb": "Page titles, meta descriptions, OG/Twitter cards, JSON-LD.",
        },
    },
    "pm": {
        "ticket_workflow": {
            "primary": "implement-jira",
            "fallbacks": [],
            "keywords": ["jira", "ticket", "prd"],
            "blurb": "Fetches a ticket, runs PRD interview, scaffolds a branch.",
        },
        "spec_writing": {
            "primary": None,
            "fallbacks": [],
            "keywords": ["prd", "spec", "product", "requirement"],
            "blurb": "Drafts PRDs and product specs.",
        },
    },
    "engineer": {
        "review": {
            "primary": "review",
            "fallbacks": [],
            "keywords": ["review", "pull request", "code review"],
            "blurb": "Reviews a pull request.",
        },
        "security_review": {
            "primary": "security-review",
            "fallbacks": [],
            "keywords": ["security", "audit"],
            "blurb": "Security review of pending changes.",
        },
        "react_perf": {
            "primary": "vercel-react-best-practices",
            "fallbacks": [],
            "keywords": ["react", "next", "performance"],
            "blurb": "React/Next perf and best practices.",
        },
    },
}


def flatten_cwd(cwd: str) -> str:
    p = cwd.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    return "-" + re.sub(r"[/.,\s]", "-", p)


def list_enabled_skills() -> list[str]:
    if not CLAUDE_SKILLS.is_dir():
        return []
    out: list[str] = []
    for entry in CLAUDE_SKILLS.iterdir():
        if entry.is_symlink() or entry.is_dir():
            out.append(entry.name)
    return sorted(out)


def skill_bytes(skill_name: str) -> int:
    md = AGENTS_SKILLS / skill_name / "SKILL.md"
    if md.is_file():
        return md.stat().st_size
    md_alt = AGENTS_SKILLS / skill_name / "skill" / "SKILL.md"
    if md_alt.is_file():
        return md_alt.stat().st_size
    return 0


def skill_md_path(skill_name: str) -> Path | None:
    md = AGENTS_SKILLS / skill_name / "SKILL.md"
    if md.is_file():
        return md
    md_alt = AGENTS_SKILLS / skill_name / "skill" / "SKILL.md"
    if md_alt.is_file():
        return md_alt
    return None


def candidate_matches_keywords(cand: dict, keywords: list[str]) -> int:
    if not keywords:
        return 0
    name = (cand.get("name") or "").lower()
    desc = (cand.get("description") or "").lower()
    hay = f"{name} {desc}"
    return sum(1 for kw in keywords if kw in hay)


def pick_for_facet(facet_name: str, facet_def: dict, enabled: set[str], candidates: list[dict]) -> dict:
    primary = facet_def.get("primary")
    fallbacks = facet_def.get("fallbacks") or []
    keywords = facet_def.get("keywords") or []

    if primary and primary in enabled:
        return {
            "name": facet_name,
            "status": "covered",
            "picked_skill": primary,
            "source": "installed:primary",
            "rationale": f"Primary pick '{primary}' is already enabled.",
        }
    for fb in fallbacks:
        if fb in enabled:
            return {
                "name": facet_name,
                "status": "covered_via_fallback",
                "picked_skill": fb,
                "source": "installed:fallback",
                "rationale": f"Primary '{primary}' not installed; using fallback '{fb}'.",
            }

    if primary and (AGENTS_SKILLS / primary / "SKILL.md").is_file():
        return {
            "name": facet_name,
            "status": "needs_enable",
            "picked_skill": primary,
            "source": "available:primary",
            "rationale": f"Primary '{primary}' source is on disk but not symlinked — enable to cover this facet.",
        }

    ranked = sorted(
        ((c, candidate_matches_keywords(c, keywords)) for c in candidates),
        key=lambda x: -x[1],
    )
    best = next(((c, score) for c, score in ranked if score > 0), None)
    if best:
        cand, score = best
        return {
            "name": facet_name,
            "status": "needs_install",
            "picked_skill": cand.get("name"),
            "source": f"candidate:{cand.get('source_provider')}",
            "rationale": (
                f"No installed skill covers this facet. Closest match in discovery pool: "
                f"'{cand.get('name')}' (keyword score {score}, source {cand.get('source_provider')})."
            ),
            "source_url": cand.get("source_url") or cand.get("html_url"),
        }

    return {
        "name": facet_name,
        "status": "uncovered",
        "picked_skill": None,
        "source": None,
        "rationale": "No installed skill, no candidate match. Manual hunt needed.",
    }


def estimate_tokens(b: int) -> int:
    return b // BYTES_PER_TOKEN


def read_project_memory(cwd: str) -> list[dict]:
    flat = flatten_cwd(cwd)
    mem_dir = PROJECTS_DIR / flat / "memory"
    out: list[dict] = []
    if not mem_dir.is_dir():
        return out
    for md in sorted(mem_dir.glob("*.md")):
        if md.name == "MEMORY.md":
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        name_m = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        desc_m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        if name_m:
            out.append({
                "name": name_m.group(1).strip(),
                "description": desc_m.group(1).strip() if desc_m else "",
                "path": str(md),
            })
    return out


def derive_project_context(signals: dict, corrections: dict, cwd: str) -> dict:
    project_bucket = (signals.get("projects") or {}).get(cwd) or {}
    if not project_bucket and signals.get("scope") == "global":
        bucket = Counter()
        ext_bucket = Counter()
        for proj in (signals.get("projects") or {}).values():
            bucket.update(proj.get("in_window_tool_calls") or {})
            ext_bucket.update(proj.get("extensions") or {})
        top_tools = bucket.most_common(8)
        top_exts = ext_bucket.most_common(5)
        global_fallback = True
    else:
        top_tools = sorted((project_bucket.get("in_window_tool_calls") or {}).items(), key=lambda x: -x[1])[:8]
        top_exts = sorted((project_bucket.get("extensions") or {}).items(), key=lambda x: -x[1])[:5]
        global_fallback = False

    memory_rules = read_project_memory(cwd)

    correction_evidence: list[str] = []
    for cand in (corrections.get("candidates") or []):
        if cand.get("count", 0) >= 3 and cand.get("kind") == "negation":
            snippets = cand.get("sample_snippets", [])
            if snippets:
                correction_evidence.append(snippets[0][:140])

    return {
        "global_fallback": global_fallback,
        "top_tools": top_tools,
        "top_extensions": top_exts,
        "memory_rules": memory_rules,
        "correction_snippets": correction_evidence[:5],
    }


def build_personalization_block(skill: str, role: str, ctx: dict) -> str:
    lines: list[str] = []
    header = "## Project context (auto-tune)\n\nThis section is auto-managed; the upstream SKILL.md above is untouched.\n"

    if ctx["global_fallback"]:
        lines.append("- This is a fresh project folder with no transcripts yet. Personalization is drawn from this user's global usage patterns.")
    else:
        lines.append(f"- This project's recent activity: top tools {[t for t,_ in ctx['top_tools'][:4]]}, top file extensions {[e for e,_ in ctx['top_extensions'][:3]]}.")

    if ctx["memory_rules"]:
        lines.append("- Project rules captured in memory:")
        for r in ctx["memory_rules"][:5]:
            lines.append(f"  - **{r['name']}** — {r['description']}")

    if ctx["correction_snippets"]:
        lines.append("- Repeating user-correction patterns observed across sessions (avoid these mistakes):")
        for s in ctx["correction_snippets"][:3]:
            lines.append(f"  - \"{s}\"")

    role_hint = {
        "designer": "- Reader is a product designer. Bias outputs toward visual fidelity, component reuse, and accessibility over framework gymnastics. When in doubt, render a screenshot description before code.",
        "pm": "- Reader is a product manager. Bias outputs toward decisions and stakeholder framing; keep code edits minimal.",
        "engineer": "- Reader is an engineer. Bias outputs toward small diffs, test coverage, and clear root-cause explanations.",
    }.get(role)
    if role_hint:
        lines.append(role_hint)

    body = "\n".join(lines)
    return header + "\n" + body + "\n"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--signals", default=str(CACHE_DIR / "signals.json"))
    p.add_argument("--candidates", default=str(CACHE_DIR / "candidates.json"))
    p.add_argument("--corrections", default=str(CACHE_DIR / "corrections.json"))
    p.add_argument("--out", default=str(CACHE_DIR / "composition.json"))
    p.add_argument("--target-tokens", type=int, default=DEFAULT_TARGET_TOKENS)
    args = p.parse_args(argv)

    role_facets = FACETS.get(args.role)
    if not role_facets:
        json.dump({"error": f"no facet map for role {args.role!r}"}, sys.stdout, indent=2)
        return 1

    signals = json.loads(Path(args.signals).read_text(encoding="utf-8")) if Path(args.signals).is_file() else {}
    candidates_obj = json.loads(Path(args.candidates).read_text(encoding="utf-8")) if Path(args.candidates).is_file() else {}
    candidates = candidates_obj.get("candidates", [])
    corrections = json.loads(Path(args.corrections).read_text(encoding="utf-8")) if Path(args.corrections).is_file() else {}

    enabled = set(list_enabled_skills())
    facet_picks = [pick_for_facet(name, defn, enabled, candidates) for name, defn in role_facets.items()]

    bundle_skills: set[str] = set()
    bundle_actions: list[dict] = []
    for fp in facet_picks:
        skill = fp.get("picked_skill")
        if not skill:
            continue
        bundle_skills.add(skill)
        if fp["status"] == "covered" or fp["status"] == "covered_via_fallback":
            bundle_actions.append({"skill": skill, "action": "keep", "facet": fp["name"]})
        elif fp["status"] == "needs_enable":
            bundle_actions.append({"skill": skill, "action": "enable_symlink", "facet": fp["name"]})
        elif fp["status"] == "needs_install":
            bundle_actions.append({
                "skill": skill,
                "action": "install_then_enable",
                "facet": fp["name"],
                "source_url": fp.get("source_url"),
            })

    not_in_bundle = sorted(enabled - bundle_skills - {"auto-tune", "find-skills"})
    for sk in not_in_bundle:
        bundle_actions.append({"skill": sk, "action": "disable_in_folder", "facet": None})

    before_bytes = sum(skill_bytes(sk) for sk in enabled)
    after_bytes = sum(skill_bytes(sk) for sk in bundle_skills if (AGENTS_SKILLS / sk / "SKILL.md").is_file())
    delta = before_bytes - after_bytes
    token_budget = {
        "target_tokens": args.target_tokens,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "delta_bytes": delta,
        "est_tokens_before": estimate_tokens(before_bytes),
        "est_tokens_after": estimate_tokens(after_bytes),
        "est_tokens_saved": estimate_tokens(delta),
        "within_target": estimate_tokens(after_bytes) <= args.target_tokens,
    }

    ctx = derive_project_context(signals, corrections, args.cwd)
    personalizations: list[dict] = []
    for sk in sorted(bundle_skills):
        md = skill_md_path(sk)
        if not md:
            continue
        try:
            current = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "## Project context (auto-tune)" in current:
            continue
        block = build_personalization_block(sk, args.role, ctx)
        personalizations.append({
            "skill": sk,
            "target_path": str(md),
            "context_block": block,
            "evidence": {
                "global_fallback": ctx["global_fallback"],
                "memory_rules_count": len(ctx["memory_rules"]),
                "correction_count": len(ctx["correction_snippets"]),
            },
        })

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "role": args.role,
        "cwd": args.cwd,
        "facets": facet_picks,
        "bundle_actions": bundle_actions,
        "token_budget": token_budget,
        "personalizations": personalizations,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": args.out,
        "facets": len(facet_picks),
        "covered": sum(1 for f in facet_picks if f["status"].startswith("covered")),
        "needs_enable": sum(1 for f in facet_picks if f["status"] == "needs_enable"),
        "needs_install": sum(1 for f in facet_picks if f["status"] == "needs_install"),
        "uncovered": sum(1 for f in facet_picks if f["status"] == "uncovered"),
        "bundle_actions": len(bundle_actions),
        "personalizations": len(personalizations),
        "token_budget": token_budget,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
