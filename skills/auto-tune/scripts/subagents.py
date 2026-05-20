#!/usr/bin/env python3
"""Subagent chain composer for auto-tune.

For a given role, fills out a set of subagent templates (orchestrator + specialists),
substituting:
  - project context (memory rules + correction patterns + role hint)
  - the user's currently-installed skills (so each subagent references real ones)
  - the user's currently-available MCPs (so 'Connected now' lists are accurate)
  - manual-paste data sources for missing analytics MCPs (Pendo / Mixpanel / Dovetail / Mobbin)

Output: cache/subagent_drafts.json with one entry per subagent
  (target_path, body, rationale, phase, missing-skill/MCP diagnostics).

propose.py reads this file and emits `gen-subagent` items.
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
TEMPLATES_DIR = SKILL_ROOT / "prompts" / "subagent_templates"
CLAUDE_AGENTS = HOME / ".claude" / "agents"
CLAUDE_SKILLS = HOME / ".claude" / "skills"
CLAUDE_JSON = HOME / ".claude.json"
PROJECTS_DIR = HOME / ".claude" / "projects"


# Per-role specialist chain.  Order is meaningful: the orchestrator goes first;
# specialists are listed in workflow order.  Each spec declares its preferred
# MCPs (only "connected now" if installed) and skill dependencies.
CHAINS: dict[str, list[dict]] = {
    "designer": [
        {"name": "designer-fullstack", "template": "fullstack.md.tmpl", "phase": "orchestrator",
         "preferred_mcps": [], "preferred_skills": [],
         "placeholders": ["{{PROJECT_CONTEXT_BLOCK}}"]},
        {"name": "designer-researcher", "template": "researcher.md.tmpl", "phase": "research",
         "preferred_mcps": ["atlassian", "chrome-devtools", "figma-dev"],
         "preferred_skills": [],
         "placeholders": ["{{PROJECT_CONTEXT_BLOCK}}", "{{CONNECTED_DATA_SOURCES}}"]},
        {"name": "designer-spec-writer", "template": "spec-writer.md.tmpl", "phase": "spec",
         "preferred_mcps": ["atlassian"],
         "preferred_skills": ["impeccable", "emil-design-eng"],
         "placeholders": ["{{PROJECT_CONTEXT_BLOCK}}", "{{SPEC_WRITER_SKILLS}}"]},
        {"name": "designer-implementer", "template": "implementer.md.tmpl", "phase": "implementation",
         "preferred_mcps": ["chrome-devtools", "figma-dev"],
         "preferred_skills": ["baseline-ui", "vercel-react-best-practices"],
         "placeholders": ["{{PROJECT_CONTEXT_BLOCK}}", "{{IMPLEMENTER_SKILLS}}"]},
        {"name": "designer-polish-reviewer", "template": "polish-reviewer.md.tmpl", "phase": "polish",
         "preferred_mcps": ["chrome-devtools"],
         "preferred_skills": ["impeccable", "emil-design-eng", "fixing-accessibility",
                              "fixing-motion-performance", "fixing-metadata"],
         "placeholders": ["{{PROJECT_CONTEXT_BLOCK}}", "{{REVIEWER_SKILLS}}"]},
        {"name": "designer-handoff", "template": "handoff.md.tmpl", "phase": "handoff",
         "preferred_mcps": ["atlassian"],
         "preferred_skills": [],
         "placeholders": ["{{PROJECT_CONTEXT_BLOCK}}", "{{CONNECTED_DATA_SOURCES}}"]},
    ],
    # v5 — placeholders only
    "pm": [],
    "engineer": [],
}

MCP_DESCRIPTIONS = {
    "atlassian": "Confluence + Jira (Atlassian MCP)",
    "chrome-devtools": "Live browser inspection (chrome-devtools MCP)",
    "figma-dev": "Figma file access (figma-dev MCP)",
    "github": "GitHub repo access (github MCP)",
    "slack": "Slack channels + messages (slack MCP)",
    "linear": "Linear issue tracking (linear MCP)",
}


def flatten_cwd(cwd: str) -> str:
    p = cwd.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    return "-" + re.sub(r"[/.,\s]", "-", p)


def installed_skills() -> list[str]:
    if not CLAUDE_SKILLS.is_dir():
        return []
    out: list[str] = []
    for entry in CLAUDE_SKILLS.iterdir():
        if entry.is_symlink() or entry.is_dir():
            if entry.name in ("auto-tune",):
                continue
            out.append(entry.name)
    return sorted(out)


def installed_mcps() -> list[str]:
    if not CLAUDE_JSON.is_file():
        return []
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return sorted((data.get("mcpServers") or {}).keys())


def read_memory_rules(cwd: str) -> list[dict]:
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
            })
    return out


def read_correction_snippets() -> list[str]:
    p = CACHE_DIR / "corrections.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[str] = []
    for cand in data.get("candidates", []):
        if cand.get("count", 0) >= 3:
            snips = cand.get("sample_snippets", [])
            if snips:
                out.append(snips[0][:140])
    return out[:5]


def build_project_context_block(role: str, cwd: str) -> str:
    lines: list[str] = [
        "This section is auto-managed by auto-tune; the template above stays untouched.",
        "",
    ]
    mem = read_memory_rules(cwd)
    if mem:
        lines.append("Project rules from memory:")
        for r in mem[:5]:
            lines.append(f"- **{r['name']}** — {r['description']}")
    corr = read_correction_snippets()
    if corr:
        if mem:
            lines.append("")
        lines.append("Repeating user-correction patterns to avoid:")
        for c in corr[:3]:
            lines.append(f"- \"{c}\"")
    role_hint = {
        "designer": "Reader works as a product designer. Bias outputs toward visual fidelity, component reuse, and accessibility over framework gymnastics.",
        "pm": "Reader works as a product manager. Bias toward decisions and stakeholder framing; keep code edits minimal.",
        "engineer": "Reader works as an engineer. Bias toward small diffs, test coverage, and root-cause explanations.",
    }.get(role)
    if role_hint:
        if mem or corr:
            lines.append("")
        lines.append(role_hint)
    return "\n".join(lines)


def build_connected_data_sources(preferred_mcps: list[str], available_mcps: list[str]) -> str:
    enabled = [m for m in preferred_mcps if m in available_mcps]
    if not enabled:
        return "- (none of this subagent's preferred MCPs are currently installed; rely on manual-paste data sources below or ask the user to install one)"
    return "\n".join(f"- {MCP_DESCRIPTIONS.get(m, m)}" for m in enabled)


def build_skill_invocation_list(preferred_skills: list[str], installed: list[str]) -> str:
    available = [s for s in preferred_skills if s in installed]
    if not available:
        return "- (none of this subagent's preferred skills are currently installed; rely on your own judgment and flag the gap to the user)"
    return "\n".join(f"- `{s}`" for s in available)


def fill_template(role: str, spec: dict, available_skills: list[str], available_mcps: list[str],
                  ctx_block: str) -> str | None:
    tmpl_path = TEMPLATES_DIR / role / spec["template"]
    if not tmpl_path.is_file():
        return None
    body = tmpl_path.read_text(encoding="utf-8")
    connected = build_connected_data_sources(spec.get("preferred_mcps", []), available_mcps)
    skills_list = build_skill_invocation_list(spec.get("preferred_skills", []), available_skills)
    body = body.replace("{{ROLE}}", role)
    body = body.replace("{{PROJECT_CONTEXT_BLOCK}}", ctx_block)
    body = body.replace("{{CONNECTED_DATA_SOURCES}}", connected)
    body = body.replace("{{SPEC_WRITER_SKILLS}}", skills_list)
    body = body.replace("{{IMPLEMENTER_SKILLS}}", skills_list)
    body = body.replace("{{REVIEWER_SKILLS}}", skills_list)
    return body


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--out", default=str(CACHE_DIR / "subagent_drafts.json"))
    args = p.parse_args(argv)

    chain = CHAINS.get(args.role)
    if not chain:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"role": args.role, "drafts": [], "note": "no chain defined for this role"}, indent=2), encoding="utf-8")
        print(json.dumps({"role": args.role, "drafts": 0, "note": "no chain"}, indent=2))
        return 0

    skills = installed_skills()
    mcps = installed_mcps()
    ctx_block = build_project_context_block(args.role, args.cwd)

    drafts: list[dict] = []
    for spec in chain:
        body = fill_template(args.role, spec, skills, mcps, ctx_block)
        if body is None:
            continue
        target_path = CLAUDE_AGENTS / f"{spec['name']}.md"
        connected = [m for m in spec.get("preferred_mcps", []) if m in mcps]
        missing_mcps = [m for m in spec.get("preferred_mcps", []) if m not in mcps]
        usable_skills = [s for s in spec.get("preferred_skills", []) if s in skills]
        missing_skills = [s for s in spec.get("preferred_skills", []) if s not in skills]
        rationale_parts = [f"phase: {spec['phase']}"]
        if connected:
            rationale_parts.append(f"MCPs connected: {','.join(connected)}")
        if usable_skills:
            rationale_parts.append(f"skills referenced: {','.join(usable_skills)}")
        if missing_skills:
            rationale_parts.append(f"missing skills: {','.join(missing_skills)}")
        drafts.append({
            "name": spec["name"],
            "target_path": str(target_path),
            "body": body,
            "bytes": len(body.encode("utf-8")),
            "phase": spec["phase"],
            "rationale": "; ".join(rationale_parts),
            "preferred_mcps_connected": connected,
            "preferred_mcps_missing": missing_mcps,
            "preferred_skills_available": usable_skills,
            "preferred_skills_missing": missing_skills,
        })

    total_bytes = sum(d["bytes"] for d in drafts)
    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "role": args.role,
        "cwd": args.cwd,
        "chain_size": len(drafts),
        "total_bytes": total_bytes,
        "est_tokens": total_bytes // 4,
        "available_skills": skills,
        "available_mcps": mcps,
        "drafts": drafts,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    summary = {k: v for k, v in out.items() if k != "drafts"}
    summary["draft_names"] = [d["name"] for d in drafts]
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
