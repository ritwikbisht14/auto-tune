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

    # Safety rule: if a CLAUDE.md exists but does NOT carry the auto-tune marker,
    # treat it as team-authored and never overwrite. Propose an additive appendix
    # instead so the team's text is preserved.
    auto_tune_marker = "<!-- auto-tune-generated"
    file_is_team_authored = before and auto_tune_marker not in before
    if file_is_team_authored:
        # Build an additive appendix that adds only role-specific guidance + the
        # auto-tune marker. We deliberately drop the full template body — the
        # team's existing CLAUDE.md already establishes the project's conventions.
        appendix_lines = [
            "",
            "<!-- auto-tune-generated-appendix: do not delete this comment. auto-tune appended the section below; the team's content above stays untouched. -->",
            "",
            f"## auto-tune addendum (role: {role})",
            "",
        ]
        if rules:
            appendix_lines.append("Project rules from memory:")
            for title, desc in rules:
                appendix_lines.append(f"- **{title}** — {desc}")
            appendix_lines.append("")
        if role_section:
            appendix_lines.append(role_section)
        appendix = "\n".join(appendix_lines).rstrip() + "\n"

        # Skip if our appendix is already present (idempotent).
        if "auto-tune-generated-appendix" in before:
            return proposals
        proposals.append({
            "id": "append-claude-md:project",
            "type": "append-claude-md",
            "scope": "project",
            "target_path": str(target),
            "before": before,
            "after": before.rstrip() + "\n\n" + appendix,
            "appendix": appendix,
            "rationale": (
                f"role={role}, {project_bucket['session_count']} sessions. "
                "Existing CLAUDE.md appears team-authored (no auto-tune marker). "
                "Proposing an additive appendix; team content stays intact."
            ),
            "est_token_savings": 0,
        })
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


def _load_facets_for_role(role: str) -> dict:
    """v5.3: lazy-import compose.FACETS so we can group external candidates by facet."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "auto_tune_compose",
            Path(__file__).resolve().parent / "compose.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.FACETS.get(role) or {}
    except Exception:  # noqa: BLE001
        return {}


def _infer_facet(candidate: dict, role_facets: dict) -> str | None:
    """v5.3: infer which facet this candidate belongs to by matching its
    description against each facet's keywords. Returns the best-matching facet
    name, or None if no facet shows ≥1 keyword overlap."""
    if not role_facets:
        return None
    # Curated seeds already carry a facet_hint.
    if candidate.get("facet_hint"):
        return candidate["facet_hint"]
    text = f"{candidate.get('name','')} {candidate.get('description','')}".lower()
    best_facet = None
    best_score = 0
    for facet_name, facet_def in role_facets.items():
        keywords = facet_def.get("keywords", [])
        if not keywords:
            continue
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_facet = facet_name
    return best_facet if best_score >= 1 else None


def _why_this_line(c: dict) -> str:
    """v5.3: one-line rationale highlighting the strongest signal contributors."""
    qc = c.get("quality_components") or {}
    parts: list[str] = []
    if c.get("source_provider") == "curated":
        parts.append("curated editorial pick")
        if c.get("_curated_rationale"):
            return "Why: " + c["_curated_rationale"]
    if qc.get("trust"):
        parts.append("trusted author")
    cross_boost = qc.get("cross_provider_boost", 0)
    if cross_boost >= 0.10:
        cnt = c.get("cross_provider_count", 0)
        parts.append(f"in {cnt} providers")
    pop = c.get("popularity") or 0
    if pop >= 50:
        parts.append(f"{pop} {c.get('popularity_kind','stars')}")
    if qc.get("active_maintenance_bonus", 0) > 0:
        parts.append("active (last 90d)")
    if qc.get("structure", 0) >= 0.5 or qc.get("structure_contrib", 0) >= 0.10:
        parts.append("real SKILL.md + scaffolding")
    fb = qc.get("feedback_adjustment", 0)
    if fb >= 0.15:
        parts.append("matches your prior installs")
    elif fb <= -0.15:
        parts.append("⚠ near a prior rejection")
    if not parts:
        parts.append(f"role-relevance {qc.get('role_relevance', 0):.2f}")
    return "Why: " + " • ".join(parts)


def propose_external_candidates(candidates_path: Path, role: str = "designer",
                                 max_per_facet: int = 3,
                                 show_all: bool = False) -> list[dict]:
    if not candidates_path.is_file():
        return []
    try:
        data = json.loads(candidates_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    proposals: list[dict] = []
    role_facets = _load_facets_for_role(role)

    # v5.3: bucket candidates by facet first; cap each bucket later.
    # v5.4: defensive — skip any candidate whose deep-inspect found no SKILL.md.
    # discover.py drops these at scoring time, but this is belt-and-suspenders
    # for stale candidates.json files generated by pre-v5.4 versions.
    by_facet: dict[str | None, list[dict]] = {}
    skipped_no_skill_md = 0
    for c in data.get("candidates", []):
        if c.get("security_status") not in ("clean", "skipped(dry-run)"):
            continue
        name = c.get("name", "").strip()
        url = c.get("source_url", "")
        if not name or not url:
            continue
        # Curated seeds bypass the SKILL.md check (they're editorial picks with
        # qs=1.0 and the user has confirmed they want them surfaced).
        if c.get("source_provider") != "curated":
            insp = c.get("inspection") or {}
            if c.get("inspection") is not None and not insp.get("skill_md_path"):
                skipped_no_skill_md += 1
                continue
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48] or "external"
        target = AGENTS_SKILLS / slug / "SKILL.md"
        if target.exists():
            continue
        facet = _infer_facet(c, role_facets)
        by_facet.setdefault(facet, []).append({**c, "_slug": slug, "_target": str(target)})

    # Sort each facet bucket by quality_score desc, cap at max_per_facet.
    overflow_count = 0
    for facet, items in by_facet.items():
        items.sort(key=lambda x: -x.get("quality_score", 0.0))
        kept = items if show_all else items[:max_per_facet]
        overflow_count += max(0, len(items) - len(kept))
        for c in kept:
            slug = c["_slug"]
            target_str = c["_target"]
            url = c.get("source_url", "")
            name = c.get("name", "")
            provider = c.get("source_provider", "external")
            qc = c.get("quality_components") or {}
            pop = c.get("popularity") or 0
            pop_kind = c.get("popularity_kind") or "stars"
            last_act = c.get("last_activity_at") or "?"

            if c.get("security_status") == "clean" and c.get("quarantine_sha256"):
                kind = "add-skill-external"
                after_desc = (
                    f"# {name}\n\n"
                    f"Source: {url}\nProvider: {provider}\n"
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

            facet_label = facet or "uncategorized"
            why_line = _why_this_line(c)
            rationale = (
                f"[{facet_label}] {provider} • quality {c.get('quality_score', 0):.2f} "
                f"• role-rel {qc.get('role_relevance', c.get('role_relevance', 0)):.2f} "
                f"• {pop} {pop_kind} • last {last_act[:10] if last_act else '?'}\n"
                f"{why_line}"
            )

            proposals.append({
                "id": f"{kind}:{slug}",
                "type": kind,
                "scope": "global",
                "target_path": target_str,
                "after": after_desc,
                "rationale": rationale,
                "est_token_savings": 0,
                "enable_command": f"ln -s {AGENTS_SKILLS / slug} {CLAUDE_DIR / 'skills' / slug}",
                "source_url": url,
                "quarantine_sha256": c.get("quarantine_sha256"),
                "popularity": pop,
                "popularity_kind": pop_kind,
                "last_activity_at": last_act,
                "quality_score": c.get("quality_score"),
                "facet": facet,
                "candidate_name": name,
            })

    # v5.3: surface filter diagnostics as a synthetic item so the user sees what was dropped.
    diag = data.get("diagnostics") or []
    if diag or overflow_count:
        filter_counts: dict[str, int] = {}
        for d in diag:
            for k in d.keys():
                if k.startswith("_"):
                    filter_counts[k] = filter_counts.get(k, 0) + 1
        filter_summary = ", ".join(f"{c} {k.lstrip('_')}" for k, c in sorted(filter_counts.items(), key=lambda x: -x[1])[:6])
        summary_text = (
            f"Discovery summary: {len(proposals)} candidates surfaced across "
            f"{len([f for f in by_facet if f])} role facets.\n"
            f"- Filtered out: {filter_summary or '(none)'}\n"
            f"- v5.4 SKILL.md gate: {skipped_no_skill_md} candidates without a SKILL.md were skipped at propose time.\n"
            f"- Capped: {overflow_count} candidates below the top-{max_per_facet}-per-facet cut "
            f"(run with --show-all to see the full list)."
        )
        proposals.insert(0, {
            "id": "discovery-summary",
            "type": "discovery-summary",
            "scope": "info",
            "target_path": "(info)",
            "after": summary_text,
            "rationale": "v5.3: read-only summary of what discovery filtered/kept.",
            "est_token_savings": 0,
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


def propose_subagents(drafts_path: Path) -> list[dict]:
    if not drafts_path.is_file():
        return []
    try:
        data = json.loads(drafts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    proposals: list[dict] = []
    for draft in data.get("drafts", []):
        target = Path(draft["target_path"])
        proposals.append({
            "id": f"gen-subagent:{draft['name']}",
            "type": "gen-subagent",
            "scope": "global",
            "target_path": str(target),
            "after": draft["body"],
            "rationale": draft.get("rationale", ""),
            "est_token_savings": 0,
            "phase": draft.get("phase"),
            "preferred_mcps_missing": draft.get("preferred_mcps_missing", []),
            "preferred_skills_missing": draft.get("preferred_skills_missing", []),
            "bytes": draft.get("bytes", len(draft["body"])),
        })
    return proposals


def propose_upgrades(upgrades_path: Path) -> list[dict]:
    if not upgrades_path.is_file():
        return []
    try:
        data = json.loads(upgrades_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items: list[dict] = []
    for u in data.get("upgrades", []):
        cand = u.get("candidate") or {}
        installed = u.get("installed", "")
        cand_name = cand.get("name", "")
        if not installed or not cand_name:
            continue
        items.append({
            "id": f"swap-skill:{installed}:{slugify(cand_name)}",
            "type": "swap-skill",
            "scope": "global",
            "target_path": str(HOME / ".claude" / "skills" / installed),
            "before": installed,
            "after": cand_name,
            "rationale": (
                f"Better candidate found in facet '{u.get('facet')}': "
                f"{cand_name} scored {cand.get('quality_score', 0):.2f}, "
                f"delta {u.get('score_delta', 0):+.2f} over installed baseline. "
                f"Source: {cand.get('source_url', '?')}"
            ),
            "est_token_savings": 0,
            "facet": u.get("facet"),
            "source_url": cand.get("source_url"),
            "candidate": cand,
        })
    return items


def propose_restores(drift_path: Path) -> list[dict]:
    if not drift_path.is_file():
        return []
    try:
        data = json.loads(drift_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items: list[dict] = []
    for c in data.get("candidates", []):
        name = c.get("pruned_name", "")
        if not name:
            continue
        if not c.get("source_exists", False):
            # Source was actually deleted — can't restore. Skip.
            continue
        snippets = c.get("evidence_snippets", [])
        evidence_preview = "; ".join(s[:80] for s in snippets[:2])
        items.append({
            "id": f"restore-skill:{name}",
            "type": "restore-skill",
            "scope": "global",
            "target_path": str(HOME / ".claude" / "skills" / name),
            "before": "(symlink missing)",
            "after": f"symlink → {HOME / '.agents' / 'skills' / name}",
            "rationale": (
                f"You mentioned topics this skill covers {c.get('occurrence_count', 0)} times "
                f"since it was pruned on {c.get('pruned_at', '?')}. "
                f"Matched keywords: {', '.join(c.get('keywords_matched', [])[:5])}. "
                f"Evidence: \"{evidence_preview}\""
            ),
            "est_token_savings": 0,
            "pruned_at": c.get("pruned_at"),
            "occurrence_count": c.get("occurrence_count"),
        })
    return items


def propose_branch_isolate(cwd: str, enabled: bool, will_write_project_files: bool) -> list[dict]:
    """Emit a branch-isolate item when the user opted in AND we'll be writing project files."""
    if not enabled or not will_write_project_files:
        return []
    project = Path(cwd)
    if not (project / ".git").exists():
        return []  # Not a git repo; nothing to ignore.
    gitignore = project / ".gitignore"
    fenced_block = (
        "\n# auto-tune: begin (personal Claude Code config; do not commit)\n"
        ".claude/CLAUDE.md\n"
        ".claude/settings.local.json\n"
        ".claude/agents/\n"
        "# auto-tune: end\n"
    )
    existing = ""
    if gitignore.is_file():
        existing = gitignore.read_text(encoding="utf-8", errors="ignore")
        if "# auto-tune: begin" in existing:
            return []  # Already isolated, idempotent.
    return [{
        "id": "branch-isolate:gitignore",
        "type": "branch-isolate",
        "scope": "project",
        "target_path": str(gitignore),
        "before": "(no fenced block)",
        "after": fenced_block.strip(),
        "rationale": (
            "Appends a fenced block to .gitignore so per-project auto-tune writes "
            "(CLAUDE.md, settings.local.json, agents/) stay in your tree only and "
            "don't merge into the team branch."
        ),
        "est_token_savings": 0,
    }]


def propose_missing_skill_gaps(role: str, drafts_path: Path) -> list[dict]:
    """Surface external skills the user needs to find for the subagent chain.

    For role=designer the subagent chain references specific skill names; if any
    are missing from the user's installed set, emit a `manual-find-skill` item
    pointing them at the right kind of search.
    """
    if not drafts_path.is_file():
        return []
    try:
        data = json.loads(drafts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    role_specific_gaps = {
        "designer": [
            ("ux-writing", "UX writing / microcopy skill", "Used by designer-content. Search: 'claude skill ux writing', 'claude code microcopy'."),
            ("figma-export", "Figma export / figma-to-code skill", "Used by designer-researcher + designer-implementer for pulling real frames. Search: 'claude skill figma export'."),
            ("design-system-audit", "Design-system audit / component-inventory skill", "Used by designer-implementer to scout existing components. Search: 'claude code design system audit', 'component inventory skill'."),
            ("user-research-synthesis", "User-research synthesis skill", "Used by designer-researcher to parse Dovetail / interview transcripts. Search: 'claude skill user research synthesis'."),
        ],
    }
    gaps = role_specific_gaps.get(role, [])
    if not gaps:
        return []
    missing_skills_in_drafts: set[str] = set()
    for draft in data.get("drafts", []):
        for s in draft.get("preferred_skills_missing", []):
            missing_skills_in_drafts.add(s)
    items: list[dict] = []
    for slug, title, search_hint in gaps:
        items.append({
            "id": f"manual-find-skill:{slug}",
            "type": "manual-find-skill",
            "scope": "manual",
            "target_path": "(external)",
            "before": "(not installed)",
            "after": f"User finds and installs a {title.lower()}",
            "rationale": (
                f"{title} would meaningfully improve the {role} chain. "
                f"{search_hint} Paste the GitHub URL back to /auto-tune and it will quarantine-fetch + scan."
            ),
            "est_token_savings": 0,
        })
    return items


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
    p.add_argument("--subagents", default=str(Path(__file__).resolve().parent.parent / "cache" / "subagent_drafts.json"))
    p.add_argument("--upgrades", default=str(Path(__file__).resolve().parent.parent / "cache" / "upgrades.json"))
    p.add_argument("--drift", default=str(Path(__file__).resolve().parent.parent / "cache" / "drift.json"))
    p.add_argument("--with-security-hook", action="store_true")
    p.add_argument("--with-branch-isolate", action="store_true")
    p.add_argument("--max-per-facet", type=int, default=3,
                   help="v5.3: cap external candidates surfaced per facet (default 3)")
    p.add_argument("--show-all", action="store_true",
                   help="v5.3: include all external candidates, not just top per facet")
    args = p.parse_args(argv)

    signals = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    cwd = str(Path(args.cwd).expanduser().resolve())

    items: list[dict] = []
    items += propose_skill_prunes(signals, args.role, cwd)
    items += propose_mcp_actions(signals, args.role, cwd)
    items += propose_claude_md(signals, args.role, cwd)
    items += propose_skill_stubs(signals, args.role, cwd)
    items += propose_external_candidates(
        Path(args.candidates),
        role=args.role,
        max_per_facet=args.max_per_facet,
        show_all=args.show_all,
    )
    items += propose_tweaks(Path(args.corrections))
    items += propose_personalizations(Path(args.composition))
    items += propose_compose_summary(Path(args.composition))
    items += propose_subagents(Path(args.subagents))
    items += propose_upgrades(Path(args.upgrades))
    items += propose_restores(Path(args.drift))
    items += propose_missing_skill_gaps(args.role, Path(args.subagents))
    will_write_project_files = any(
        i.get("type") in ("gen-claude-md", "prune-skill", "prune-mcp")
        and i.get("scope") == "project"
        for i in items
    )
    items += propose_branch_isolate(cwd, args.with_branch_isolate, will_write_project_files)
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
            "append_claude_md": sum(1 for i in items if i["type"] == "append-claude-md"),
            "gen_skill": sum(1 for i in items if i["type"] == "gen-skill"),
            "add_skill_external": sum(1 for i in items if i["type"] == "add-skill-external"),
            "recommend_agent_external": sum(1 for i in items if i["type"] == "recommend-agent-external"),
            "tweak_skill": sum(1 for i in items if i["type"] == "tweak-skill"),
            "personalize_skill": sum(1 for i in items if i["type"] == "personalize-skill"),
            "compose_bundle": sum(1 for i in items if i["type"] == "compose-bundle"),
            "gen_subagent": sum(1 for i in items if i["type"] == "gen-subagent"),
            "add_hook": sum(1 for i in items if i["type"] == "add-hook"),
            "swap_skill": sum(1 for i in items if i["type"] == "swap-skill"),
            "restore_skill": sum(1 for i in items if i["type"] == "restore-skill"),
            "branch_isolate": sum(1 for i in items if i["type"] == "branch-isolate"),
            "manual_find_skill": sum(1 for i in items if i["type"] == "manual-find-skill"),
            "discovery_summary": sum(1 for i in items if i["type"] == "discovery-summary"),
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
