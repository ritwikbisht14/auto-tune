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
         "placeholders": []},
        {"name": "designer-ideation", "template": "ideation.md.tmpl", "phase": "ideation",
         "preferred_mcps": [],
         "preferred_skills": [],
         "placeholders": ["{{CONNECTED_DATA_SOURCES}}"]},
        {"name": "designer-researcher", "template": "researcher.md.tmpl", "phase": "research",
         "preferred_mcps": ["atlassian", "chrome-devtools", "figma-dev", "slack"],
         "preferred_skills": [],
         "placeholders": ["{{CONNECTED_DATA_SOURCES}}"]},
        {"name": "designer-content", "template": "content.md.tmpl", "phase": "callable-anywhere",
         "preferred_mcps": ["atlassian"],
         "preferred_skills": [],
         "placeholders": [],
         "requires_mcp": "atlassian"},
        {"name": "designer-spec-writer", "template": "spec-writer.md.tmpl", "phase": "spec",
         "preferred_mcps": ["atlassian"],
         "preferred_skills": ["impeccable", "emil-design-eng"],
         "placeholders": ["{{SPEC_WRITER_SKILLS}}"]},
        {"name": "designer-implementer", "template": "implementer.md.tmpl", "phase": "implementation",
         "preferred_mcps": ["chrome-devtools", "figma-dev"],
         "preferred_skills": ["baseline-ui", "vercel-react-best-practices"],
         "placeholders": ["{{IMPLEMENTER_SKILLS}}"]},
        {"name": "designer-polish-reviewer", "template": "polish-reviewer.md.tmpl", "phase": "polish",
         "preferred_mcps": ["chrome-devtools"],
         "preferred_skills": ["impeccable", "emil-design-eng", "fixing-accessibility",
                              "fixing-motion-performance", "fixing-metadata"],
         "placeholders": ["{{REVIEWER_SKILLS}}"]},
        {"name": "designer-handoff", "template": "handoff.md.tmpl", "phase": "handoff",
         "preferred_mcps": ["atlassian"],
         "preferred_skills": [],
         "placeholders": ["{{CONNECTED_DATA_SOURCES}}"]},
    ],
    # v6 — placeholders only
    "pm": [],
    "engineer": [],
}

# claude.ai-managed MCPs that satisfy a "logical" MCP name (e.g. "atlassian").
# Subagents declare preferred_mcps in human terms; this map lets subagents.py
# detect that a claude.ai-managed equivalent is connected and counts as "atlassian".
MCP_ALIASES = {
    "atlassian": ["atlassian", "claude_ai_Atlassian_Rovo"],
    "slack": ["slack", "claude_ai_Slack"],
    "microsoft-365": ["microsoft-365", "claude_ai_Microsoft_365"],
    "zoom": ["zoom", "claude_ai_Zoom_for_Claude"],
}

MCP_DESCRIPTIONS = {
    "atlassian": "Confluence + Jira (Atlassian MCP)",
    "chrome-devtools": "Live browser inspection (chrome-devtools MCP)",
    "figma-dev": "Figma file access (figma-dev MCP)",
    "github": "GitHub repo access (github MCP)",
    "slack": "Slack channels + messages (Slack MCP — read-only for feedback mining)",
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


CONNECTED_MCPS_FILE = SKILL_ROOT / "security" / "connected_mcps.txt"


def installed_mcps() -> list[str]:
    """Return the list of MCPs the user has access to.

    Sources, merged:
      - ~/.claude.json mcpServers (self-installed local MCPs)
      - ~/.agents/skills/auto-tune/security/connected_mcps.txt (claude.ai-managed
        MCPs the user has confirmed authenticated — the auth cache only tracks
        unauthenticated MCPs, so we need an explicit list for connected ones)

    Names returned are the *raw* MCP names (e.g. 'claude_ai_Atlassian_Rovo',
    'chrome-devtools'); callers use MCP_ALIASES to map logical names
    ('atlassian') to whatever is actually connected.
    """
    out: set[str] = set()
    if CLAUDE_JSON.is_file():
        try:
            data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
            out.update((data.get("mcpServers") or {}).keys())
        except json.JSONDecodeError:
            pass
    if CONNECTED_MCPS_FILE.is_file():
        try:
            for line in CONNECTED_MCPS_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    out.add(line)
        except OSError:
            pass
    return sorted(out)


def mcp_is_connected(logical_name: str, available: list[str]) -> bool:
    """True if any alias for logical_name appears in available."""
    aliases = MCP_ALIASES.get(logical_name, [logical_name])
    return any(a in available for a in aliases)


def build_connected_data_sources(preferred_mcps: list[str], available_mcps: list[str]) -> str:
    enabled = [m for m in preferred_mcps if mcp_is_connected(m, available_mcps)]
    if not enabled:
        return "- (none of this subagent's preferred MCPs are currently installed; rely on manual-paste data sources below or ask the user to install one)"
    return "\n".join(f"- {MCP_DESCRIPTIONS.get(m, m)}" for m in enabled)


def build_skill_invocation_list(preferred_skills: list[str], installed: list[str]) -> str:
    available = [s for s in preferred_skills if s in installed]
    if not available:
        return "- (none of this subagent's preferred skills are currently installed; rely on your own judgment and flag the gap to the user)"
    return "\n".join(f"- `{s}`" for s in available)


CONFIG_DIR = SKILL_ROOT / "config"


def _designer_content_config() -> dict | None:
    """Load the user's designer-content Confluence config (gitignored)."""
    p = CONFIG_DIR / "designer-content.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _designer_content_unconfigured_block() -> str:
    """Markdown that explains the user how to configure designer-content."""
    example = CONFIG_DIR / "designer-content.json.example"
    return (
        "> **⚠️ designer-content is not yet configured for this user.**\n"
        ">\n"
        f"> Copy [`config/designer-content.json.example`]({example}) to `config/designer-content.json`,\n"
        "> fill in your company's Confluence `cloud_id`, `folder_id`, and the page IDs of your UX copy\n"
        "> guidelines, then re-run `/auto-tune`. Until that's done this subagent will operate on its own\n"
        "> design judgment without your company's voice/tone references.\n"
    )


def _build_page_catalog(cfg: dict) -> str:
    """Render the Markdown page-catalog table from the JSON config."""
    lines = ["| Page ID | Title | Load when |", "|---|---|---|"]
    for p in cfg.get("always_load_pages", []):
        lines.append(f"| **{p['id']}** | **{p['title']}** | **Always (baseline)** |")
    for p in cfg.get("conditional_pages", []):
        lines.append(f"| {p['id']} | {p['title']} | {p.get('load_when', '—')} |")
    return "\n".join(lines)


def _apply_designer_content_placeholders(body: str) -> str:
    """Substitute Confluence placeholders in content.md.tmpl using the config file."""
    cfg = _designer_content_config()
    if cfg is None:
        # Replace placeholders with a clear "configure me" notice.
        notice = _designer_content_unconfigured_block()
        body = body.replace("{{CONFLUENCE_CLOUD_ID}}", "<not-configured>")
        body = body.replace("{{UX_COPY_FOLDER_ID}}", "<not-configured>")
        body = body.replace("{{PAGE_CATALOG}}", notice)
        return body
    body = body.replace("{{CONFLUENCE_CLOUD_ID}}", cfg.get("cloud_id", ""))
    body = body.replace("{{UX_COPY_FOLDER_ID}}", str(cfg.get("folder_id", "")))
    body = body.replace("{{PAGE_CATALOG}}", _build_page_catalog(cfg))
    return body


def fill_template(role: str, spec: dict, available_skills: list[str], available_mcps: list[str]) -> str | None:
    tmpl_path = TEMPLATES_DIR / role / spec["template"]
    if not tmpl_path.is_file():
        return None
    body = tmpl_path.read_text(encoding="utf-8")
    connected = build_connected_data_sources(spec.get("preferred_mcps", []), available_mcps)
    skills_list = build_skill_invocation_list(spec.get("preferred_skills", []), available_skills)
    body = body.replace("{{ROLE}}", role)
    body = body.replace("{{CONNECTED_DATA_SOURCES}}", connected)
    body = body.replace("{{SPEC_WRITER_SKILLS}}", skills_list)
    body = body.replace("{{IMPLEMENTER_SKILLS}}", skills_list)
    body = body.replace("{{REVIEWER_SKILLS}}", skills_list)
    if spec.get("name") == "designer-content":
        body = _apply_designer_content_placeholders(body)
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

    drafts: list[dict] = []
    for spec in chain:
        body = fill_template(args.role, spec, skills, mcps)
        if body is None:
            continue
        target_path = CLAUDE_AGENTS / f"{spec['name']}.md"
        connected = [m for m in spec.get("preferred_mcps", []) if mcp_is_connected(m, mcps)]
        missing_mcps = [m for m in spec.get("preferred_mcps", []) if not mcp_is_connected(m, mcps)]
        usable_skills = [s for s in spec.get("preferred_skills", []) if s in skills]
        missing_skills = [s for s in spec.get("preferred_skills", []) if s not in skills]
        rationale_parts = [f"phase: {spec['phase']}"]
        if connected:
            rationale_parts.append(f"MCPs connected: {','.join(connected)}")
        if usable_skills:
            rationale_parts.append(f"skills referenced: {','.join(usable_skills)}")
        if missing_skills:
            rationale_parts.append(f"missing skills: {','.join(missing_skills)}")
        requires_mcp = spec.get("requires_mcp")
        blocked_reason = None
        if requires_mcp and not mcp_is_connected(requires_mcp, mcps):
            blocked_reason = f"requires {requires_mcp} MCP — not connected. Run /mcp and authenticate before installing this subagent."
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
            "blocked_reason": blocked_reason,
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
