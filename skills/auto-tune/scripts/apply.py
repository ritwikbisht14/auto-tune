#!/usr/bin/env python3
"""Apply an approved subset of a proposal.

Writes one audit line per change to ~/.agents/skills/auto-tune/cache/log.jsonl.
Refuses to touch ~/.claude.json or anything under ~/.claude/projects/*/memory/.

Usage:
  apply.py --proposal cache/proposal.json --approved <id1,id2,...> [--dry-run]
  apply.py --proposal cache/proposal.json --all [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

HOME = Path.home()
LOG_PATH = Path(__file__).resolve().parent.parent / "cache" / "log.jsonl"
FORBIDDEN_PATH_FRAGMENTS = (
    str(HOME / ".claude.json"),
    "/.claude/projects/",
)


def is_forbidden(path: str) -> bool:
    abs_path = str(Path(path).expanduser().resolve()) if path and not path.startswith("(") else path
    for frag in FORBIDDEN_PATH_FRAGMENTS:
        if frag in abs_path:
            return True
    return False


def append_log(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def apply_prune_skill(item: dict, dry_run: bool) -> dict:
    target = Path(item["target_path"])
    if item["scope"] == "global":
        if not target.is_symlink() and not target.exists():
            return {"status": "skipped", "reason": "symlink already absent"}
        if dry_run:
            return {"status": "dry-run", "would": f"unlink {target}"}
        backup = str(os.readlink(target)) if target.is_symlink() else None
        target.unlink()
        return {"status": "applied", "undo": f"ln -s {backup} {target}" if backup else f"recreate symlink at {target}"}
    settings_path = target
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    skill_name = item.get("skill") or item["id"].split(":")[1]
    disabled = list(data.get("disabledSkills") or [])
    if skill_name in disabled:
        return {"status": "skipped", "reason": "already disabled"}
    disabled.append(skill_name)
    data["disabledSkills"] = disabled
    if dry_run:
        return {"status": "dry-run", "would": f"add {skill_name!r} to disabledSkills in {settings_path}"}
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"status": "applied", "undo": f"remove {skill_name!r} from disabledSkills in {settings_path}"}


def apply_prune_mcp(item: dict, dry_run: bool) -> dict:
    settings_path = Path(item["target_path"])
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    mcp_name = item.get("mcp_name") or item["id"].split(":")[1]
    disabled = list(data.get("disabledMcpjsonServers") or [])
    if mcp_name in disabled:
        return {"status": "skipped", "reason": "already disabled"}
    disabled.append(mcp_name)
    data["disabledMcpjsonServers"] = disabled
    if dry_run:
        return {"status": "dry-run", "would": f"add {mcp_name!r} to disabledMcpjsonServers in {settings_path}"}
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"status": "applied", "undo": f"remove {mcp_name!r} from disabledMcpjsonServers in {settings_path}"}


def apply_gen_claude_md(item: dict, dry_run: bool) -> dict:
    target = Path(item["target_path"])
    if dry_run:
        return {"status": "dry-run", "would": f"write {target} ({len(item['after'])} bytes)"}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        backup = target.with_suffix(target.suffix + ".autotune.bak")
        backup.write_text(target.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        undo = f"mv {backup} {target}"
    else:
        undo = f"rm {target}"
    target.write_text(item["after"], encoding="utf-8")
    return {"status": "applied", "undo": undo}


def apply_gen_skill(item: dict, dry_run: bool) -> dict:
    target = Path(item["target_path"])
    if target.is_file():
        return {"status": "skipped", "reason": "skill source already exists"}
    if dry_run:
        return {"status": "dry-run", "would": f"create {target}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(item["after"], encoding="utf-8")
    return {"status": "applied", "undo": f"rm -r {target.parent}", "enable_command": item.get("enable_command")}


def apply_add_mcp(item: dict, dry_run: bool) -> dict:
    return {
        "status": "manual",
        "command": item["after"],
        "note": "auto-tune does not edit ~/.claude.json; run the command yourself to install.",
    }


def apply_add_skill_external(item: dict, dry_run: bool) -> dict:
    target = Path(item["target_path"])
    sha = item.get("quarantine_sha256")
    quarantine_root = Path(__file__).resolve().parent.parent / "security" / "quarantine"
    if not sha:
        return {"status": "skipped", "reason": "no quarantine sha; not safe to promote"}
    src = quarantine_root / sha / "content"
    if not src.is_file():
        return {"status": "skipped", "reason": f"quarantine file missing for sha {sha}"}

    if target.exists():
        return {"status": "skipped", "reason": "skill source already exists"}

    if dry_run:
        return {"status": "dry-run", "would": f"copy {src} → {target}"}

    target.parent.mkdir(parents=True, exist_ok=True)
    body = src.read_text(encoding="utf-8", errors="replace")
    header = (
        f"---\nname: {target.parent.name}\n"
        f"description: Imported by auto-tune from {item.get('source_url', 'external source')}.\n---\n\n"
    )
    if not body.lstrip().startswith("---"):
        body = header + body
    target.write_text(body, encoding="utf-8")
    return {
        "status": "applied",
        "undo": f"rm -r {target.parent}",
        "enable_command": item.get("enable_command"),
        "note": "Skill source written but NOT symlinked. Review the file, then enable manually.",
    }


def apply_recommend_agent_external(item: dict, dry_run: bool) -> dict:
    return {
        "status": "manual",
        "url": item.get("source_url"),
        "note": "Recommendation only. Open the URL, review, and install if you trust it.",
    }


def apply_tweak_skill(item: dict, dry_run: bool) -> dict:
    target = Path(item["target_path"])
    if not target.is_file():
        return {"status": "skipped", "reason": "target SKILL.md missing"}
    current = target.read_text(encoding="utf-8", errors="ignore")
    if current != item.get("before"):
        return {"status": "skipped", "reason": "target file changed since proposal generated"}
    after = item["after"]
    if dry_run:
        return {"status": "dry-run", "would": f"append constraint to {target} (+{len(after) - len(current)} chars)"}
    backup = target.with_suffix(target.suffix + ".autotune.bak")
    backup.write_text(current, encoding="utf-8")
    target.write_text(after, encoding="utf-8")
    return {"status": "applied", "undo": f"mv {backup} {target}"}


def apply_add_hook(item: dict, dry_run: bool) -> dict:
    settings_path = Path(item["target_path"])
    hook_cmd = item.get("hook_command")
    if not hook_cmd:
        return {"status": "skipped", "reason": "no hook_command in proposal"}
    data: dict = {}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    hooks = data.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    entry = {
        "matcher": "WebFetch|Bash",
        "hooks": [{"type": "command", "command": hook_cmd}],
    }
    if any(json.dumps(h) == json.dumps(entry) for h in pre):
        return {"status": "skipped", "reason": "hook already configured"}
    pre.append(entry)
    if dry_run:
        return {"status": "dry-run", "would": f"add PreToolUse hook to {settings_path}"}
    backup = settings_path.with_suffix(settings_path.suffix + ".autotune.bak")
    if settings_path.is_file():
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"status": "applied", "undo": f"mv {backup} {settings_path}" if backup.exists() else f"rm {settings_path}"}


def apply_personalize_skill(item: dict, dry_run: bool) -> dict:
    target = Path(item["target_path"])
    if not target.is_file():
        return {"status": "skipped", "reason": "target SKILL.md missing"}
    current = target.read_text(encoding="utf-8", errors="ignore")
    if "## Project context (auto-tune)" in current:
        return {"status": "skipped", "reason": "project-context block already present"}
    if current != item.get("before"):
        return {"status": "skipped", "reason": "target changed since proposal generated"}
    after = item["after"]
    if dry_run:
        return {"status": "dry-run", "would": f"append project-context to {target} (+{len(after) - len(current)} chars)"}
    backup = target.with_suffix(target.suffix + ".autotune.bak")
    backup.write_text(current, encoding="utf-8")
    target.write_text(after, encoding="utf-8")
    return {"status": "applied", "undo": f"mv {backup} {target}"}


def apply_compose_bundle(item: dict, dry_run: bool) -> dict:
    return {
        "status": "manual",
        "note": "Read-only summary of the composed bundle; individual actions land via prune-skill / add-skill-external / personalize-skill items.",
    }


AUTO_TUNE_SUBAGENT_MARKER = "## Project context (auto-tune)"


def apply_gen_subagent(item: dict, dry_run: bool) -> dict:
    target = Path(item["target_path"])
    new_body = item["after"]
    if target.is_file():
        existing = target.read_text(encoding="utf-8", errors="ignore")
        if AUTO_TUNE_SUBAGENT_MARKER not in existing:
            return {
                "status": "skipped",
                "reason": "target was manually authored or modified (no auto-tune marker present); pass --force to overwrite",
            }
        backup = target.with_suffix(target.suffix + ".autotune.bak")
        if dry_run:
            return {
                "status": "dry-run",
                "would": f"overwrite {target} (existing backed up to {backup.name})",
            }
        backup.write_text(existing, encoding="utf-8")
        target.write_text(new_body, encoding="utf-8")
        return {"status": "applied", "undo": f"mv {backup} {target}"}
    if dry_run:
        return {"status": "dry-run", "would": f"write {target} ({len(new_body)} bytes)"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_body, encoding="utf-8")
    return {"status": "applied", "undo": f"rm {target}"}


HANDLERS = {
    "prune-skill": apply_prune_skill,
    "prune-mcp": apply_prune_mcp,
    "gen-claude-md": apply_gen_claude_md,
    "gen-skill": apply_gen_skill,
    "add-mcp": apply_add_mcp,
    "add-skill-external": apply_add_skill_external,
    "recommend-agent-external": apply_recommend_agent_external,
    "tweak-skill": apply_tweak_skill,
    "personalize-skill": apply_personalize_skill,
    "compose-bundle": apply_compose_bundle,
    "gen-subagent": apply_gen_subagent,
    "add-hook": apply_add_hook,
}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--proposal", required=True)
    p.add_argument("--approved", default="", help="comma-separated proposal ids")
    p.add_argument("--all", dest="all_items", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    items: list[dict] = proposal.get("items", [])
    approved_ids = set(s.strip() for s in args.approved.split(",") if s.strip())

    if not args.all_items and not approved_ids:
        print(json.dumps({"error": "no items approved; pass --approved or --all"}), file=sys.stderr)
        return 2

    results: list[dict] = []
    for item in items:
        if not args.all_items and item["id"] not in approved_ids:
            continue
        handler = HANDLERS.get(item["type"])
        if not handler:
            results.append({"id": item["id"], "status": "skipped", "reason": f"unknown type {item['type']}"})
            continue
        target = item.get("target_path", "")
        if target and not target.startswith("(") and is_forbidden(target):
            results.append({"id": item["id"], "status": "refused", "reason": f"forbidden path: {target}"})
            continue
        try:
            res = handler(item, args.dry_run)
        except Exception as e:  # noqa: BLE001
            res = {"status": "error", "error": str(e)}
        out = {"id": item["id"], "type": item["type"], **res}
        results.append(out)
        if not args.dry_run and res.get("status") == "applied":
            append_log({
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "id": item["id"],
                "type": item["type"],
                "target": item.get("target_path"),
                "undo": res.get("undo"),
            })

    print(json.dumps({
        "applied": sum(1 for r in results if r["status"] == "applied"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "manual": sum(1 for r in results if r["status"] == "manual"),
        "refused": sum(1 for r in results if r["status"] == "refused"),
        "dry_run": args.dry_run,
        "results": results,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
