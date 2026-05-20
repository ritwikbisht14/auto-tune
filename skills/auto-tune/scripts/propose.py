#!/usr/bin/env python3
"""Produce a proposal.json from a signals.json + role + cwd.

Proposal items have a uniform shape:
{
  "id": "prune-skill:baseline-ui:global",
  "type": "prune-skill" | "prune-mcp" | "gen-claude-md" | "add-mcp" | "gen-skill",
  "scope": "global" | "project",
  "target_path": "...",
  "before": "...",        # optional, may be omitted for creates
  "after": "...",         # the content/state to write
  "rationale": "...",
  "est_token_savings": int  # rough; positive means savings, can be 0
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
AGENTS_SKILLS = HOME / ".agents" / "skills"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

PRUNE_RELEVANCE_THRESHOLD = 0.2

ROLE_PROFILE = {
    "designer": {
        "keywords": {"design", "figma", "ui", "ux", "css", "tailwind", "accessibility", "motion", "spec", "mock", "screenshot", "prototype", "component", "layout", "color", "typography"},
        "recommended_mcp": {"figma-dev", "chrome-devtools"},
        "skill_relevance": {"baseline-ui", "fixing-accessibility", "fixing-motion-performance", "fixing-metadata", "vercel-react-best-practices"},
    },
    "pm": {
        "keywords": {"prd", "ticket", "jira", "linear", "requirement", "stakeholder", "roadmap", "epic", "story", "spec", "doc"},
        "recommended_mcp": {"atlassian", "linear", "slack"},
        "skill_relevance": {"find-skills"},
    },
    "engineer": {
        "keywords": {"implement", "refactor", "test", "build", "ci", "deploy", "bug", "fix", "compile", "performance", "security"},
        "recommended_mcp": {"chrome-devtools", "github"},
        "skill_relevance": {"vercel-react-best-practices", "fixing-motion-performance", "find-skills"},
    },
}

MCP_HINTS = {
    "atlassian": {
        "trigger_keywords": {"jira", "atlassian", "confluence"},
        "install_command": "claude mcp add atlassian --transport http https://mcp.atlassian.com",
        "description": "Jira + Confluence access via Atlassian's MCP server.",
    },
    "linear": {
        "trigger_keywords": {"linear", "ticket"},
        "install_command": "claude mcp add linear --transport stdio -- npx -y @linear/mcp-server",
        "description": "Linear ticket queries and updates.",
    },
    "slack": {
        "trigger_keywords": {"slack", "channel", "dm "},
        "install_command": "claude mcp add slack --transport stdio -- npx -y @slack/mcp-server",
        "description": "Slack channel + message access.",
    },
    "github": {
        "trigger_keywords": {"pull request", "pr ", "issue", "github"},
        "install_command": "claude mcp add github --transport stdio -- npx -y @modelcontextprotocol/server-github",
        "description": "GitHub PR / issue / file access.",
    },
}


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:48] or "skill"


def read_skill_description(skill_name: str) -> str:
    skill_md = AGENTS_SKILLS / skill_name / "SKILL.md"
    if not skill_md.is_file():
        return ""
    text = skill_md.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def role_relevance(skill_name: str, role: str) -> float:
    desc = read_skill_description(skill_name).lower()
    if not desc:
        return 0.0
    keywords = ROLE_PROFILE.get(role, {}).get("keywords", set())
    if skill_name in ROLE_PROFILE.get(role, {}).get("skill_relevance", set()):
        return 1.0
    hits = sum(1 for kw in keywords if kw in desc)
    return min(1.0, hits / 4.0)


def load_template(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def propose_skill_prunes(signals: dict, role: str, cwd: str) -> list[dict]:
    proposals: list[dict] = []
    project_bucket = signals["projects"].get(cwd) or {}
    in_window_skills = project_bucket.get("in_window_skill_invocations", {})

    global_invocations: dict[str, int] = {}
    for proj in signals["projects"].values():
        for sk, n in (proj.get("in_window_skill_invocations") or {}).items():
            global_invocations[sk] = global_invocations.get(sk, 0) + n

    for skill in signals.get("enabled_skills", []):
        if skill == "auto-tune":
            continue
        relevance = role_relevance(skill, role)
        global_uses = global_invocations.get(skill, 0)
        project_uses = in_window_skills.get(skill, 0)

        symlink = CLAUDE_DIR / "skills" / skill
        desc = read_skill_description(skill)
        est_save = estimate_tokens(desc) + 60

        if global_uses == 0 and relevance < PRUNE_RELEVANCE_THRESHOLD:
            proposals.append({
                "id": f"prune-skill:{skill}:global",
                "type": "prune-skill",
                "scope": "global",
                "target_path": str(symlink),
                "before": "enabled (symlink present)",
                "after": "disabled (symlink removed)",
                "rationale": f"0 invocations in last 90 days across all projects; role-relevance {relevance:.2f} below threshold {PRUNE_RELEVANCE_THRESHOLD}.",
                "est_token_savings": est_save,
            })
        elif project_uses == 0 and global_uses > 0 and relevance < 0.5:
            proposals.append({
                "id": f"prune-skill:{skill}:project",
                "type": "prune-skill",
                "scope": "project",
                "target_path": str(Path(cwd) / ".claude" / "settings.local.json"),
                "before": "globally enabled, no project override",
                "after": f"add {skill!r} to disabledSkills in this project's settings.local.json",
                "rationale": f"used in other projects ({global_uses}x) but never in this folder; role-relevance {relevance:.2f}.",
                "est_token_savings": est_save,
                "skill": skill,
            })
    return proposals


def propose_mcp_actions(signals: dict, role: str, cwd: str) -> list[dict]:
    proposals: list[dict] = []
    installed = set(signals.get("mcp_servers", []))
    project_bucket = signals["projects"].get(cwd) or {}
    in_window_tools = project_bucket.get("in_window_tool_calls", {})

    all_user_text = " ".join(
        member
        for proj in signals["projects"].values()
        for cluster in proj.get("intent_clusters", [])
        for member in cluster.get("members", [])
    ).lower()

    recommended = ROLE_PROFILE.get(role, {}).get("recommended_mcp", set())
    for mcp_name, hint in MCP_HINTS.items():
        if mcp_name in installed:
            continue
        kw_hits = sum(1 for kw in hint["trigger_keywords"] if kw in all_user_text)
        if mcp_name in recommended and kw_hits >= 1:
            proposals.append({
                "id": f"add-mcp:{mcp_name}",
                "type": "add-mcp",
                "scope": "global",
                "target_path": "(manual: run install command)",
                "after": hint["install_command"],
                "rationale": f"role={role}, found {kw_hits} keyword references suggesting {mcp_name} would be useful; not currently installed.",
                "est_token_savings": 0,
                "description": hint["description"],
            })

    for mcp_name in installed:
        tool_prefix = f"mcp__{mcp_name.replace('-', '_')}"
        if not any(t.startswith(f"mcp__{mcp_name}") for t in in_window_tools):
            est_save = 300
            proposals.append({
                "id": f"prune-mcp:{mcp_name}:project",
                "type": "prune-mcp",
                "scope": "project",
                "target_path": str(Path(cwd) / ".claude" / "settings.local.json"),
                "before": "loaded globally",
                "after": f"add {mcp_name!r} to disabledMcpjsonServers in this project's settings.local.json",
                "rationale": f"no invocations of mcp__{mcp_name}__* in this project in last 90 days.",
                "est_token_savings": est_save,
                "mcp_name": mcp_name,
            })
    return proposals


def memory_rules_for_project(cwd: str) -> list[tuple[str, str]]:
    """Return [(short_title, summary_line)] from memory feedback files."""
    out: list[tuple[str, str]] = []
    flattened = "-" + re.sub(r"[/.,\s]", "-", cwd.lstrip("/").rstrip("/"))
    memory_dir = CLAUDE_DIR / "projects" / flattened / "memory"
    if not memory_dir.is_dir():
        return out
    for md in sorted(memory_dir.glob("*.md")):
        if md.name == "MEMORY.md":
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m_name = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        m_desc = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        if m_name and m_desc:
            out.append((m_name.group(1).strip(), m_desc.group(1).strip()))
    return out


def propose_claude_md(signals: dict, role: str, cwd: str) -> list[dict]:
    proposals: list[dict] = []
    project_bucket = signals["projects"].get(cwd) or {}
    if project_bucket.get("session_count", 0) < 3:
        return proposals

    template = load_template("claude_md_template.md")
    if not template:
        return proposals

    exts = project_bucket.get("extensions", {})
    top_exts = sorted(exts.items(), key=lambda x: -x[1])[:5]
    stack_line = ", ".join(f"{e} ({n})" for e, n in top_exts) or "(no file edits detected)"

    rules = memory_rules_for_project(cwd)
    rules_block = "\n".join(f"- **{title}** — {desc}" for title, desc in rules) or "- (no recorded rules yet)"

    role_section = {
        "designer": "## Designer guardrails\n- Never push to shared branches without explicit per-instance confirmation.\n- When wiring chat overlays, prefer minimizing the sidebar (`shrunk`), not hiding it (`collapsed`).\n- Prefer existing components over creating new ones; check the component library first.",
        "pm": "## PM guardrails\n- Reference the ticket/PRD when proposing changes; do not invent requirements.\n- Keep written deliverables (PRDs, specs) concise and decision-oriented.\n- Confirm before sending Slack/email or commenting on tickets.",
        "engineer": "## Engineer guardrails\n- Run tests before declaring a change complete; surface failures verbatim.\n- Prefer the smallest diff that fixes the root cause; avoid drive-by refactors.\n- Confirm before destructive git operations (reset --hard, force-push, branch -D).",
    }.get(role, "")

    after = (
        template
        .replace("{{ROLE}}", role)
        .replace("{{STACK}}", stack_line)
        .replace("{{RULES}}", rules_block)
        .replace("{{ROLE_SECTION}}", role_section)
    )

    target = Path(cwd) / "CLAUDE.md"
    before = ""
    if target.is_file():
        before = target.read_text(encoding="utf-8", errors="ignore")
    elif (Path(cwd) / ".claude" / "CLAUDE.md").is_file():
        target = Path(cwd) / ".claude" / "CLAUDE.md"
        before = target.read_text(encoding="utf-8", errors="ignore")
    else:
        target = Path(cwd) / ".claude" / "CLAUDE.md"

    if before.strip() == after.strip():
        return proposals

    proposals.append({
        "id": "gen-claude-md:project",
        "type": "gen-claude-md",
        "scope": "project",
        "target_path": str(target),
        "before": before,
        "after": after,
        "rationale": f"role={role}, {project_bucket['session_count']} sessions in this folder, {len(rules)} memory rules to surface, top extensions: {stack_line}.",
        "est_token_savings": 0,
    })
    return proposals


def propose_skill_stubs(signals: dict, role: str, cwd: str) -> list[dict]:
    proposals: list[dict] = []
    template = load_template("skill_template.md")
    if not template:
        return proposals

    enabled_descs = " ".join(read_skill_description(s).lower() for s in signals.get("enabled_skills", []))

    project_bucket = signals["projects"].get(cwd) or {}
    clusters = project_bucket.get("intent_clusters", [])

    seen_slugs: set[str] = set()
    for cluster in clusters:
        if cluster["count"] < 5:
            continue
        sample = cluster["members"][0] if cluster["members"] else ""
        if not sample:
            continue
        keywords = cluster["tokens"][:6]
        if not keywords:
            continue
        overlap = sum(1 for kw in keywords if kw in enabled_descs)
        if overlap >= len(keywords) * 0.6:
            continue
        slug = f"{role}-{slugify(' '.join(keywords[:3]))}"
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        target = AGENTS_SKILLS / slug / "SKILL.md"

        after = (
            template
            .replace("{{NAME}}", slug)
            .replace("{{DESCRIPTION}}", f"{role.title()} workflow: {' '.join(keywords[:5])}. Triggered by the recurring pattern: \"{sample[:140]}\".")
            .replace("{{SAMPLE}}", sample[:280])
            .replace("{{KEYWORDS}}", ", ".join(keywords))
            .replace("{{COUNT}}", str(cluster["count"]))
        )

        proposals.append({
            "id": f"gen-skill:{slug}",
            "type": "gen-skill",
            "scope": "global",
            "target_path": str(target),
            "after": after,
            "rationale": f"recurring intent in {cluster['count']} sessions ({', '.join(keywords[:4])}); not covered by any enabled skill.",
            "est_token_savings": 0,
            "enable_command": f"ln -s {AGENTS_SKILLS / slug} {CLAUDE_DIR / 'skills' / slug}",
        })
    return proposals


def propose_external_candidates(candidates_path: Path) -> list[dict]:
    if not candidates_path.is_file():
        return []
    try:
        data = json.loads(candidates_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    proposals: list[dict] = []
    for c in data.get("candidates", []):
        if c.get("security_status") not in ("clean", "skipped(dry-run)"):
            continue
        name = c.get("name", "").strip()
        url = c.get("source_url", "")
        if not name or not url:
            continue
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48] or "external"
        provider = c.get("source_provider", "external")
        target = AGENTS_SKILLS / slug / "SKILL.md"
        if target.exists():
            continue

        if c.get("security_status") == "clean" and c.get("quarantine_sha256"):
            kind = "add-skill-external"
            after_desc = (
                f"# {name}\n\n"
                f"Source: {url}\n"
                f"Provider: {provider}\n"
                f"Description: {c.get('description', '')}\n\n"
                "(Body fetched and quarantined; promote with apply.py after manual review of the quarantined source.)\n"
            )
        else:
            kind = "recommend-agent-external"
            after_desc = (
                f"# {name}\n\n"
                f"Source: {url}\nProvider: {provider}\n"
                f"Description: {c.get('description', '')}\n"
            )

        qc = c.get("quality_components") or {}
        pop = c.get("popularity") or 0
        pop_kind = c.get("popularity_kind") or "stars"
        last_act = c.get("last_activity_at") or "?"
        rationale_parts = [
            f"quality {c.get('quality_score', 0):.2f}",
            f"role-rel {qc.get('role_relevance', c.get('role_relevance', 0)):.2f}",
            f"{pop} {pop_kind}",
            f"recency {qc.get('recency', 0):.2f} (last {last_act[:10]})",
        ]
        if qc.get("trust"):
            rationale_parts.append("trusted-author")
        insp = c.get("inspection") or {}
        structure_bits: list[str] = []
        if insp.get("skill_md_path"):
            structure_bits.append(f"SKILL.md={insp['skill_md_path']}")
        if insp.get("examples_dir"):
            structure_bits.append("examples/")
        if insp.get("tests_dir"):
            structure_bits.append("tests/")
        if insp.get("scripts_dir"):
            structure_bits.append("scripts/")
        if insp.get("has_releases"):
            structure_bits.append(f"release {insp.get('release_latest_tag','')}")
        if structure_bits:
            rationale_parts.append("structure: " + ", ".join(structure_bits))
        proposals.append({
            "id": f"{kind}:{slug}",
            "type": kind,
            "scope": "global",
            "target_path": str(target),
            "after": after_desc,
            "rationale": (
                f"{provider}: " + ", ".join(rationale_parts) +
                f"; {('quarantined clean' if c.get('quarantine_sha256') else 'link-only recommendation')}."
            ),
            "est_token_savings": 0,
            "enable_command": f"ln -s {AGENTS_SKILLS / slug} {CLAUDE_DIR / 'skills' / slug}",
            "source_url": url,
            "quarantine_sha256": c.get("quarantine_sha256"),
            "popularity": pop,
            "popularity_kind": pop_kind,
            "last_activity_at": last_act,
            "quality_score": c.get("quality_score"),
        })
    return proposals


def propose_tweaks(corrections_path: Path) -> list[dict]:
    if not corrections_path.is_file():
        return []
    try:
        data = json.loads(corrections_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    proposals: list[dict] = []
    for cand in data.get("candidates", []):
        skill = cand.get("skill")
        proposed = (cand.get("proposed_edit") or "").strip()
        if not skill or skill == "(global)" or not proposed:
            continue
        skill_md = AGENTS_SKILLS / skill / "SKILL.md"
        if not skill_md.is_file():
            continue
        before = skill_md.read_text(encoding="utf-8", errors="ignore")
        if proposed in before:
            continue
        if "## Constraints (auto-tune)" in before:
            after = before.rstrip() + "\n" + proposed.rstrip() + "\n"
        else:
            after = before.rstrip() + "\n\n## Constraints (auto-tune)\n\n" + proposed.rstrip() + "\n"
        proposals.append({
            "id": f"tweak-skill:{skill}",
            "type": "tweak-skill",
            "scope": "global",
            "target_path": str(skill_md),
            "before": before,
            "after": after,
            "rationale": (
                f"{cand.get('count', 0)} {cand.get('kind')} events attributed to '{skill}' "
                f"across {len(cand.get('session_ids', []))} sessions."
            ),
            "est_token_savings": 50,
        })
    return proposals


def propose_personalizations(composition_path: Path) -> list[dict]:
    if not composition_path.is_file():
        return []
    try:
        data = json.loads(composition_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    proposals: list[dict] = []
    for pers in data.get("personalizations", []):
        target = Path(pers["target_path"])
        if not target.is_file():
            continue
        current = target.read_text(encoding="utf-8", errors="ignore")
        if "## Project context (auto-tune)" in current:
            continue
        block = pers["context_block"]
        after = current.rstrip() + "\n\n" + block.rstrip() + "\n"
        ev = pers.get("evidence", {})
        rationale_bits = []
        if ev.get("global_fallback"):
            rationale_bits.append("derived from global usage (fresh project)")
        if ev.get("memory_rules_count"):
            rationale_bits.append(f"{ev['memory_rules_count']} memory rule(s)")
        if ev.get("correction_count"):
            rationale_bits.append(f"{ev['correction_count']} correction pattern(s)")
        rationale = "Add project-context block: " + ", ".join(rationale_bits or ["role-default guardrails"])
        proposals.append({
            "id": f"personalize-skill:{pers['skill']}",
            "type": "personalize-skill",
            "scope": "global",
            "target_path": str(target),
            "before": current,
            "after": after,
            "rationale": rationale,
            "est_token_savings": 0,
        })
    return proposals


def propose_compose_summary(composition_path: Path) -> list[dict]:
    if not composition_path.is_file():
        return []
    try:
        data = json.loads(composition_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    facets = data.get("facets", [])
    actions = data.get("bundle_actions", [])
    tb = data.get("token_budget", {})
    role = data.get("role", "?")
    covered = sum(1 for f in facets if f["status"].startswith("covered"))
    summary_lines = [
        f"Proposed **{role} multi-agent bundle** — {covered}/{len(facets)} facets covered, "
        f"estimated {tb.get('est_tokens_saved',0)} tokens/turn saved when applied.",
        "",
        "Facets:",
    ]
    for f in facets:
        summary_lines.append(f"  - {f['name']}: {f['picked_skill']} ({f['status']})")
    summary_lines.append("")
    summary_lines.append("Bundle actions:")
    for a in actions:
        summary_lines.append(f"  - {a['action']}: {a['skill']} ({a.get('facet') or ''})")
    return [{
        "id": "compose-bundle:summary",
        "type": "compose-bundle",
        "scope": "global",
        "target_path": str(composition_path),
        "after": "\n".join(summary_lines),
        "rationale": f"Composer ran for role={role}; this is a read-only summary item.",
        "est_token_savings": tb.get("est_tokens_saved", 0),
    }]


def propose_security_hook() -> list[dict]:
    settings_path = CLAUDE_DIR / "settings.json"
    security_py = Path(__file__).resolve().parent / "security.py"
    hook_cmd = f"python3 {security_py} check-url"
    existing = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    hooks = (existing.get("hooks") or {})
    pre_tool_use = hooks.get("PreToolUse") or []
    already = any(
        isinstance(h, dict) and hook_cmd in json.dumps(h)
        for h in pre_tool_use
    )
    if already:
        return []
    return [{
        "id": "add-hook:security-check-url",
        "type": "add-hook",
        "scope": "global",
        "target_path": str(settings_path),
        "before": json.dumps(existing.get("hooks") or {}, indent=2),
        "after": (
            "adds a PreToolUse hook running "
            f"`{hook_cmd} <url>` on WebFetch and Bash(curl|wget) so every internet fetch is gated by auto-tune's allowlist."
        ),
        "rationale": "extends auto-tune's URL allowlist to all Claude Code internet activity, not just /auto-tune runs.",
        "est_token_savings": 0,
        "hook_command": hook_cmd,
    }]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--signals", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--candidates", default=str(Path(__file__).resolve().parent.parent / "cache" / "candidates.json"))
    p.add_argument("--corrections", default=str(Path(__file__).resolve().parent.parent / "cache" / "corrections.json"))
    p.add_argument("--composition", default=str(Path(__file__).resolve().parent.parent / "cache" / "composition.json"))
    p.add_argument("--with-security-hook", action="store_true")
    args = p.parse_args(argv)

    signals = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    cwd = str(Path(args.cwd).expanduser().resolve())

    items: list[dict] = []
    items += propose_skill_prunes(signals, args.role, cwd)
    items += propose_mcp_actions(signals, args.role, cwd)
    items += propose_claude_md(signals, args.role, cwd)
    items += propose_skill_stubs(signals, args.role, cwd)
    items += propose_external_candidates(Path(args.candidates))
    items += propose_tweaks(Path(args.corrections))
    items += propose_personalizations(Path(args.composition))
    items += propose_compose_summary(Path(args.composition))
    if args.with_security_hook:
        items += propose_security_hook()

    proposal = {
        "generated_at": signals.get("generated_at"),
        "role": args.role,
        "cwd": cwd,
        "scope": signals.get("scope"),
        "items": items,
        "summary": {
            "prune_skill": sum(1 for i in items if i["type"] == "prune-skill"),
            "prune_mcp": sum(1 for i in items if i["type"] == "prune-mcp"),
            "add_mcp": sum(1 for i in items if i["type"] == "add-mcp"),
            "gen_claude_md": sum(1 for i in items if i["type"] == "gen-claude-md"),
            "gen_skill": sum(1 for i in items if i["type"] == "gen-skill"),
            "add_skill_external": sum(1 for i in items if i["type"] == "add-skill-external"),
            "recommend_agent_external": sum(1 for i in items if i["type"] == "recommend-agent-external"),
            "tweak_skill": sum(1 for i in items if i["type"] == "tweak-skill"),
            "personalize_skill": sum(1 for i in items if i["type"] == "personalize-skill"),
            "compose_bundle": sum(1 for i in items if i["type"] == "compose-bundle"),
            "add_hook": sum(1 for i in items if i["type"] == "add-hook"),
            "est_total_token_savings": sum(i.get("est_token_savings", 0) for i in items),
        },
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": args.out,
        "items": len(items),
        "summary": proposal["summary"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
