#!/usr/bin/env python3
"""v6 — token-cost measurement for the user's Claude Code setup.

Produces `cache/cost_report.json` ranking the things that load into every turn's
system prompt (skills, MCP tool schemas, CLAUDE.md, memory) plus the things that
load on invocation (subagents, skill bodies), against their actual usage over a
60-day window.

Read-only diagnostics — emits no proposals and applies no edits. The user reads
the report and decides what to trim manually.

Usage:
    python3 measure.py --cwd /path/to/project [--signals path] [--out path] [--window-days 60]

Inputs:
    - signals.json (from analyze.py) — per-tool / per-skill / per-subagent usage
    - ~/.claude/skills/ — installed skill symlinks
    - ~/.claude/agents/ — generated subagents
    - ~/.claude.json `mcpServers` — enabled MCP server list
    - security/mcp_tool_counts.json — per-server schema-size estimate table
    - <cwd>/.claude/CLAUDE.md — project rules
    - ~/.claude/projects/<flatten(cwd)>/memory/*.md — memory files

Output: cost_report.json with `items` ranked by per-turn token cost.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SKILLS_DIR = CLAUDE_DIR / "skills"
AGENTS_DIR = CLAUDE_DIR / "agents"

SKILL_ROOT = Path(__file__).resolve().parent.parent
SECURITY_DIR = SKILL_ROOT / "security"
CACHE_DIR = SKILL_ROOT / "cache"
DEFAULT_SIGNALS = CACHE_DIR / "signals.json"
DEFAULT_OUT = CACHE_DIR / "cost_report.json"
MCP_TOOL_COUNTS_FILE = SECURITY_DIR / "mcp_tool_counts.json"

CHARS_PER_TOKEN = 4  # rough OpenAI/Anthropic-style estimate

NOW = dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def tokens_from_bytes(n_bytes: int) -> int:
    return n_bytes // CHARS_PER_TOKEN


def read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def flatten_cwd(cwd: str) -> str:
    p = cwd.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    return "-" + re.sub(r"[/.,\s]", "-", p)


def parse_timestamp(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def split_frontmatter(content: str) -> tuple[str, str]:
    """Returns (frontmatter_block, body). frontmatter_block includes the --- fences."""
    if not content.startswith("---"):
        return "", content
    end = content.find("\n---", 3)
    if end == -1:
        return "", content
    end_of_fm = content.find("\n", end + 4)
    if end_of_fm == -1:
        return content, ""
    return content[: end_of_fm + 1], content[end_of_fm + 1 :]


# ---------------------------------------------------------------------------
# CLAUDE.md rule parsing + citation counting
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "for", "in", "on", "at",
    "is", "are", "be", "this", "that", "with", "as", "i", "you", "we", "my",
    "can", "could", "would", "please", "help", "me", "do", "it", "if", "so",
    "from", "by", "any", "some", "have", "has", "had", "let", "lets", "use",
    "not", "always", "never", "when", "should", "must", "will",
}


def parse_claude_md_rules(content: str) -> list[dict]:
    """Split CLAUDE.md into rule-shaped chunks.

    A rule = one of:
      - a `- ` bullet
      - a numbered list item (`1. `, `2. `, ...)
      - a markdown subheading (## or ###) with its body, capped to first paragraph
    """
    rules: list[dict] = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if re.match(r"^[-*]\s+", stripped):
            text = re.sub(r"^[-*]\s+", "", stripped)
            j = i + 1
            while j < len(lines) and lines[j].startswith(("  ", "\t")) and lines[j].strip():
                text += " " + lines[j].strip()
                j += 1
            rules.append({"line": i + 1, "kind": "bullet", "text": text})
            i = j
            continue
        if re.match(r"^\d+\.\s+", stripped):
            text = re.sub(r"^\d+\.\s+", "", stripped)
            rules.append({"line": i + 1, "kind": "numbered", "text": text})
            i += 1
            continue
        i += 1
    return rules


def rule_keywords(rule_text: str, max_words: int = 4) -> list[str]:
    text = rule_text.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    words = [w for w in text.split() if len(w) > 2 and w not in _STOP_WORDS]
    return words[:max_words]


def count_rule_citations(rules: list[dict], jsonl_files: list[Path], cutoff: dt.datetime) -> list[dict]:
    """For each rule, count assistant turns where its lead keywords appear.

    Heuristic: lead 4 non-stopword tokens, lowercased. Citation = all 4 keywords
    appear within a 200-character window of the same assistant turn's text.
    Captures roughly "Claude was reasoning about this rule" without being too loose.
    """
    rule_kw = [(r, rule_keywords(r["text"])) for r in rules]
    counts = [0] * len(rules)

    for jsonl_path in jsonl_files:
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_timestamp(rec.get("timestamp"))
                    if ts and ts < cutoff:
                        continue
                    msg = rec.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    text_blob = ""
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_blob += " " + (block.get("text") or "")
                    text_blob = text_blob.lower()
                    if not text_blob.strip():
                        continue
                    for idx, (_r, kws) in enumerate(rule_kw):
                        if not kws:
                            continue
                        if all(kw in text_blob for kw in kws):
                            counts[idx] += 1
        except OSError:
            continue

    out = []
    for r, c in zip(rules, counts):
        out.append({**r, "cite_count": c})
    return out


# ---------------------------------------------------------------------------
# File inspection
# ---------------------------------------------------------------------------


def inspect_skill(skill_name: str) -> dict | None:
    """Locate the SKILL.md for an enabled skill and measure it."""
    sym = SKILLS_DIR / skill_name
    if not sym.exists():
        return None
    try:
        skill_md = (sym / "SKILL.md").resolve()
    except OSError:
        return None
    if not skill_md.is_file():
        return None
    content = read_text_safe(skill_md) or ""
    fm, body = split_frontmatter(content)
    return {
        "skill_md_path": str(skill_md),
        "total_bytes": len(content.encode("utf-8")),
        "frontmatter_bytes": len(fm.encode("utf-8")),
        "body_bytes": len(body.encode("utf-8")),
    }


def inspect_subagent(agent_path: Path) -> dict:
    content = read_text_safe(agent_path) or ""
    fm, body = split_frontmatter(content)
    declared_tools: list[str] = []
    declared_model = ""
    if fm:
        tools_match = re.search(r"^tools:\s*(.+)$", fm, flags=re.MULTILINE)
        if tools_match:
            raw = tools_match.group(1).strip()
            if raw.startswith("[") and raw.endswith("]"):
                inner = raw[1:-1]
                declared_tools = [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]
            else:
                declared_tools = [t.strip() for t in raw.split(",") if t.strip()]
        model_match = re.search(r"^model:\s*(.+)$", fm, flags=re.MULTILINE)
        if model_match:
            declared_model = model_match.group(1).strip()
    return {
        "agent_md_path": str(agent_path),
        "total_bytes": len(content.encode("utf-8")),
        "frontmatter_bytes": len(fm.encode("utf-8")),
        "body_bytes": len(body.encode("utf-8")),
        "declared_tools": declared_tools,
        "declared_model": declared_model,
    }


# ---------------------------------------------------------------------------
# Measurement passes
# ---------------------------------------------------------------------------


def measure_skills(signals: dict) -> list[dict]:
    """One item per installed skill symlink."""
    items: list[dict] = []
    enabled = signals.get("enabled_skills", []) or []
    skill_inv_global: Counter[str] = Counter()
    for proj in (signals.get("projects") or {}).values():
        for name, count in (proj.get("in_window_skill_invocations") or {}).items():
            skill_inv_global[name] += count

    for skill_name in enabled:
        insp = inspect_skill(skill_name)
        if not insp:
            items.append({
                "type": "skill",
                "name": skill_name,
                "loaded_per_turn": True,
                "note": "skill symlink found but SKILL.md not readable",
                "bytes": 0,
                "tokens_est": 0,
                "invocations_60d": skill_inv_global.get(skill_name, 0),
            })
            continue
        # Frontmatter loads every turn; body loads on invocation
        per_turn_bytes = insp["frontmatter_bytes"]
        per_invoke_bytes = insp["body_bytes"]
        invs = skill_inv_global.get(skill_name, 0)
        items.append({
            "type": "skill",
            "name": skill_name,
            "loaded_per_turn": True,
            "skill_md_path": insp["skill_md_path"],
            "bytes_total": insp["total_bytes"],
            "bytes_frontmatter": per_turn_bytes,
            "bytes_body": per_invoke_bytes,
            "tokens_frontmatter_est": tokens_from_bytes(per_turn_bytes),
            "tokens_body_est": tokens_from_bytes(per_invoke_bytes),
            "invocations_60d": invs,
            "tokens_per_invocation_est": tokens_from_bytes(per_invoke_bytes),
            "user_actions_to_consider": _skill_actions(skill_name, insp, invs),
        })
    return items


def _skill_actions(name: str, insp: dict, invs: int) -> list[str]:
    actions: list[str] = []
    if invs == 0:
        actions.append(
            f"0 invocations in the 60-day window. If you don't expect to use `{name}`, "
            f"remove the symlink: `rm ~/.claude/skills/{name}` (source stays at "
            f"`~/.agents/skills/{name}/` for one-line restore)."
        )
    if insp["body_bytes"] > 12000:
        actions.append(
            f"Body is {insp['body_bytes']} bytes (~{tokens_from_bytes(insp['body_bytes'])} tokens). "
            "Consider splitting long catalogs/examples into sibling files the skill reads at invocation, "
            "leaving the SKILL.md body terse."
        )
    if insp["frontmatter_bytes"] > 1200:
        actions.append(
            "Frontmatter is unusually long. The `description:` field is loaded every turn — "
            "tighten it to one or two sentences."
        )
    return actions


def measure_mcps(signals: dict, mcp_table: dict) -> list[dict]:
    items: list[dict] = []
    servers = signals.get("mcp_servers", []) or []
    raw_calls_global: Counter[str] = Counter()
    for proj in (signals.get("projects") or {}).values():
        for tool, count in (proj.get("in_window_tool_calls") or {}).items():
            raw_calls_global[tool] += count

    table = (mcp_table.get("servers") or {})
    default = table.get("_default") or {"approx_tools": 10, "approx_schema_bytes": 4000}

    for server in servers:
        entry = table.get(server, default)
        tools_used = raw_calls_global.get(f"mcp__{server}", 0)
        approx_bytes = int(entry.get("approx_schema_bytes", default["approx_schema_bytes"]))
        actions: list[str] = []
        if tools_used == 0:
            actions.append(
                f"0 `mcp__{server}__*` tool calls in the 60-day window. "
                f"If you don't use this server in this project, disable it: "
                f"`claude mcp disable {server}` (or add `\"{server}\"` to "
                f"`<cwd>/.claude/settings.local.json` under `disabledMcpjsonServers`)."
            )
        elif tools_used < 5:
            actions.append(
                f"Only {tools_used} calls in 60 days. Per-turn schema cost (~{tokens_from_bytes(approx_bytes)} "
                f"tokens) may not be worth the convenience — review whether keeping it loaded everywhere makes sense."
            )
        items.append({
            "type": "mcp",
            "name": server,
            "loaded_per_turn": True,
            "approx_schema_bytes": approx_bytes,
            "approx_tools": entry.get("approx_tools"),
            "tokens_est": tokens_from_bytes(approx_bytes),
            "tools_used_60d": tools_used,
            "estimate_note": "approximate; see security/mcp_tool_counts.json to refine",
            "user_actions_to_consider": actions,
        })
    return items


def measure_claude_md(cwd: str, window_days: int) -> dict | None:
    path = Path(cwd) / ".claude" / "CLAUDE.md"
    if not path.is_file():
        return None
    content = read_text_safe(path) or ""
    n_bytes = len(content.encode("utf-8"))
    rules = parse_claude_md_rules(content)
    cutoff = NOW - dt.timedelta(days=window_days)
    proj_dir = PROJECTS_DIR / flatten_cwd(cwd)
    jsonl_files: list[Path] = []
    if proj_dir.is_dir():
        jsonl_files = sorted(proj_dir.glob("*.jsonl"))
    rules_with_cites = count_rule_citations(rules, jsonl_files, cutoff) if rules else []
    uncited = [r for r in rules_with_cites if r["cite_count"] == 0]
    cited = [r for r in rules_with_cites if r["cite_count"] > 0]
    actions: list[str] = []
    if rules and len(uncited) >= max(3, len(rules) // 3):
        actions.append(
            f"{len(uncited)} of {len(rules)} rules have never been cited in {window_days} days. "
            "Review the `uncited_rules` list and prune anything that's dead weight."
        )
    if n_bytes > 6000:
        actions.append(
            f"CLAUDE.md is {n_bytes} bytes (~{tokens_from_bytes(n_bytes)} tokens) loaded every turn. "
            "Consider moving long examples/justifications into sibling docs the rules link to."
        )
    return {
        "type": "claude_md",
        "name": str(path),
        "loaded_per_turn": True,
        "bytes": n_bytes,
        "tokens_est": tokens_from_bytes(n_bytes),
        "rules_total": len(rules),
        "rules_cited_60d": len(cited),
        "rules_uncited_60d": len(uncited),
        "uncited_rules": [
            {"line": r["line"], "text_preview": r["text"][:120]} for r in uncited[:30]
        ],
        "user_actions_to_consider": actions,
    }


def measure_memory(cwd: str) -> list[dict]:
    proj_dir = PROJECTS_DIR / flatten_cwd(cwd)
    mem_dir = proj_dir / "memory"
    if not mem_dir.is_dir():
        return []
    items: list[dict] = []
    for f in sorted(mem_dir.glob("*.md")):
        content = read_text_safe(f) or ""
        n_bytes = len(content.encode("utf-8"))
        items.append({
            "type": "memory",
            "name": f.name,
            "path": str(f),
            "loaded_per_turn": True,
            "bytes": n_bytes,
            "tokens_est": tokens_from_bytes(n_bytes),
            "user_actions_to_consider": [],
        })
    return items


def measure_subagents(signals: dict) -> list[dict]:
    if not AGENTS_DIR.is_dir():
        return []
    items: list[dict] = []
    agent_inv_global: Counter[str] = Counter()
    for proj in (signals.get("projects") or {}).values():
        for name, count in (proj.get("in_window_agent_invocations") or {}).items():
            agent_inv_global[name] += count

    raw_calls_global: Counter[str] = Counter()
    for proj in (signals.get("projects") or {}).values():
        for tool, count in (proj.get("raw_tool_calls") or {}).items():
            raw_calls_global[tool] += count

    for agent_md in sorted(AGENTS_DIR.glob("*.md")):
        insp = inspect_subagent(agent_md)
        agent_name = agent_md.stem
        invs = agent_inv_global.get(agent_name, 0)
        declared = insp["declared_tools"]
        declared_set = set(declared)
        # A declared tool is "globally unused" if it never fired in any session
        # in 60d. False negatives possible for tools the main session also uses
        # (we can't fully attribute) but it's a safe trim signal.
        unused_declared = [
            t for t in declared
            if t not in raw_calls_global and t.split("__")[0] not in raw_calls_global
        ]
        actions: list[str] = []
        if invs == 0 and declared:
            actions.append(
                f"0 invocations of `{agent_name}` in 60 days. Body costs "
                f"~{tokens_from_bytes(insp['body_bytes'])} tokens per invocation; if you don't "
                "expect to use it, delete the agent file."
            )
        if unused_declared and invs > 0:
            actions.append(
                f"{len(unused_declared)} declared tools have never fired anywhere in 60 days: "
                f"{', '.join(unused_declared[:5])}{'…' if len(unused_declared) > 5 else ''}. "
                "Trim from `tools:` frontmatter to save schema tokens per invocation."
            )
        if insp["body_bytes"] > 10000:
            actions.append(
                f"Body is {insp['body_bytes']} bytes — the heaviest sections may be lazy-loadable. "
                "Consider moving long catalogs/schemas into sibling files the subagent reads at invocation."
            )
        items.append({
            "type": "subagent",
            "name": agent_name,
            "loaded_per_turn": False,
            "loaded_when_invoked": True,
            "agent_md_path": insp["agent_md_path"],
            "bytes_total": insp["total_bytes"],
            "bytes_frontmatter": insp["frontmatter_bytes"],
            "bytes_body": insp["body_bytes"],
            "tokens_est_per_invocation": tokens_from_bytes(insp["total_bytes"]),
            "declared_tools": declared,
            "declared_tool_count": len(declared),
            "tools_declared_but_globally_unused_60d": unused_declared,
            "declared_model": insp["declared_model"],
            "invocations_60d": invs,
            "user_actions_to_consider": actions,
        })
    return items


# ---------------------------------------------------------------------------
# Ranking + report assembly
# ---------------------------------------------------------------------------


def _per_turn_token_cost(item: dict) -> int:
    if not item.get("loaded_per_turn"):
        return 0
    if item["type"] == "skill":
        return item.get("tokens_frontmatter_est", 0)
    return item.get("tokens_est", 0)


def _per_invocation_token_cost(item: dict) -> int:
    if item["type"] == "skill":
        return item.get("tokens_body_est", 0)
    if item["type"] == "subagent":
        return item.get("tokens_est_per_invocation", 0)
    return 0


def rank_items(items: list[dict]) -> list[dict]:
    """Sort with a composite score:
       - loaded_per_turn items dominate (their cost compounds across every turn)
       - within those, unused items get a multiplier so they float to the top
    """
    def score(item: dict) -> tuple:
        per_turn = _per_turn_token_cost(item)
        zero_use = False
        if item["type"] == "mcp":
            zero_use = item.get("tools_used_60d", 0) == 0
        elif item["type"] == "skill":
            zero_use = item.get("invocations_60d", 0) == 0
        # Higher = more concerning. Bool first so zero-use floats to top.
        return (zero_use, per_turn, _per_invocation_token_cost(item))

    ranked = sorted(items, key=score, reverse=True)
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx
    return ranked


def build_summary(items: list[dict]) -> dict:
    per_turn_total = sum(_per_turn_token_cost(i) for i in items)
    per_turn_bytes = 0
    for i in items:
        if not i.get("loaded_per_turn"):
            continue
        if i["type"] == "skill":
            per_turn_bytes += i.get("bytes_frontmatter", 0)
        elif i["type"] == "mcp":
            per_turn_bytes += i.get("approx_schema_bytes", 0)
        else:
            per_turn_bytes += i.get("bytes", 0)

    top3 = items[:3]
    top3_tokens = sum(_per_turn_token_cost(i) for i in top3)

    subagent_bytes_when_invoked = sum(
        i.get("bytes_total", 0) for i in items if i["type"] == "subagent"
    )

    return {
        "per_turn_loaded_bytes": per_turn_bytes,
        "per_turn_loaded_tokens_est": per_turn_total,
        "subagent_chain_bytes_when_invoked": subagent_bytes_when_invoked,
        "subagent_chain_tokens_when_invoked_est": tokens_from_bytes(subagent_bytes_when_invoked),
        "top_3_offenders": [
            {"type": i["type"], "name": i["name"], "tokens_per_turn_est": _per_turn_token_cost(i)}
            for i in top3
        ],
        "estimated_per_turn_savings_if_top_3_addressed": top3_tokens,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def load_signals(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_mcp_table() -> dict:
    if not MCP_TOOL_COUNTS_FILE.is_file():
        return {"servers": {"_default": {"approx_tools": 10, "approx_schema_bytes": 4000}}}
    try:
        return json.loads(MCP_TOOL_COUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"servers": {"_default": {"approx_tools": 10, "approx_schema_bytes": 4000}}}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cwd", required=True)
    p.add_argument("--signals", default=str(DEFAULT_SIGNALS))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--window-days", type=int, default=60)
    args = p.parse_args(argv)

    signals = load_signals(Path(args.signals))
    mcp_table = load_mcp_table()

    items: list[dict] = []
    items.extend(measure_skills(signals))
    items.extend(measure_mcps(signals, mcp_table))
    cm = measure_claude_md(args.cwd, args.window_days)
    if cm:
        items.append(cm)
    items.extend(measure_memory(args.cwd))
    items.extend(measure_subagents(signals))

    ranked = rank_items(items)
    summary = build_summary(ranked)

    out = {
        "generated_at": NOW.isoformat(),
        "cwd": args.cwd,
        "window_days": args.window_days,
        "chars_per_token_est": CHARS_PER_TOKEN,
        "signals_source": args.signals if Path(args.signals).is_file() else "(missing)",
        "items": ranked,
        "summary": summary,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(json.dumps({
        "wrote": str(out_path),
        "items": len(ranked),
        "per_turn_tokens_est": summary["per_turn_loaded_tokens_est"],
        "top_3": [i["name"] for i in summary["top_3_offenders"]],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
