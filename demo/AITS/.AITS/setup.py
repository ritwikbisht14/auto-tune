#!/usr/bin/env python3
"""Operator script — DO NOT show this to the demo audience.

Synthesizes Claude Code transcripts under
`~/.claude/projects/<flatten(demo-cwd)>/` so when `/auto-tune` runs from the
parent folder it has 60 days of believable activity to chew on. The transcripts
reflect a role you pick (designer / pm / engineer).

This lives in the hidden `.demo/` subfolder so the parent demo folder looks
like a real project to Claude Code (no demo-flavored README at the root).

Usage (run from anywhere):
    python3 ~/auto-tune-demo/.demo/setup.py            # interactive role prompt
    python3 ~/auto-tune-demo/.demo/setup.py --role designer
    python3 ~/auto-tune-demo/.demo/setup.py --role engineer --sessions 30
    python3 ~/auto-tune-demo/.demo/setup.py --reset    # wipe synthetic transcripts

After setup, `cd ~/auto-tune-demo && claude`, then `/auto-tune --cost-report`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import uuid
from pathlib import Path

HOME = Path.home()
# Demo folder is the parent of .demo/
DEMO_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = HOME / ".claude" / "projects"


def flatten_cwd(cwd: str) -> str:
    p = cwd.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    return "-" + re.sub(r"[/.,\s]", "-", p)


ROLE_PROFILES = {
    "designer": {
        "frequent_tools": ["Read", "Write", "Edit", "Glob", "Grep"],
        "rare_tools": ["Bash", "WebSearch"],
        "skills_used": ["impeccable", "baseline-ui", "fixing-accessibility", "emil-design-eng"],
        "skills_unused": [
            "fixing-metadata",
            "fixing-motion-performance",
            "vercel-react-best-practices",
            "find-skills",
        ],
        "agents_used": ["designer-researcher", "designer-spec-writer", "designer-implementer"],
        "mcp_calls": {
            "mcp__claude_ai_Atlassian_Rovo": ["getConfluencePage", "searchConfluenceUsingCql"],
            "mcp__figma-dev": ["get_figma_data"],
        },
        "mcp_unused": ["chrome-devtools", "claude_ai_Slack"],
        "file_exts": [".tsx", ".css", ".tsx", ".tsx", ".ts", ".md", ".tsx", ".css", ".tsx"],
        "intents": [
            "audit the accessibility on the modal component",
            "design a new empty state for the dashboard",
            "rewrite the error message copy for the form",
            "review the design tokens for spacing consistency",
            "create a spec for the sidebar redesign",
            "add aria-labels to the icon-only buttons",
            "update the button hierarchy in the component library",
            "check contrast ratios on the new banner variants",
        ],
        "cite_phrases": [
            "I'll make sure the contrast ratio meets WCAG AA standards on these.",
            "Using the design tokens from src/styles/tokens.css for spacing.",
            "Adding proper aria-labels to all interactive elements.",
            "Following the existing button hierarchy for the new variant.",
            "Matched the copy tone to the warm, plain voice and tone guidelines.",
            "Component variants are data-attribute-driven, not class-name-driven.",
            "Used a sibling .css file colocated with the component.",
            "Typed props explicitly with TypeScript interfaces.",
        ],
    },
    "engineer": {
        "frequent_tools": ["Bash", "Edit", "Read", "Grep", "Glob"],
        "rare_tools": ["WebSearch"],
        "skills_used": ["vercel-react-best-practices"],
        "skills_unused": [
            "impeccable",
            "baseline-ui",
            "fixing-accessibility",
            "fixing-metadata",
            "fixing-motion-performance",
            "emil-design-eng",
            "find-skills",
        ],
        "agents_used": [],
        "mcp_calls": {
            "mcp__chrome-devtools": ["take_screenshot", "list_network_requests", "evaluate_script"],
        },
        "mcp_unused": ["figma-dev", "claude_ai_Atlassian_Rovo"],
        "file_exts": [".ts", ".ts", ".py", ".js", ".tsx", ".test.ts", ".test.tsx", ".ts"],
        "intents": [
            "fix the failing unit tests in the auth module",
            "refactor the database connection pool",
            "add error handling to the payment processor",
            "investigate the memory leak in the background worker",
            "tighten the TypeScript types in the API client",
            "set up a CI workflow for the integration tests",
        ],
        "cite_phrases": [
            "Running npm run build to verify no type errors before committing.",
            "Adding explicit TypeScript interfaces for these props.",
            "Co-locating the component CSS as a sibling .css file.",
            "Tests run with vitest as the standard for this repo.",
            "Avoided importing from node_modules paths directly.",
            "Squash-merging this feature branch into develop.",
        ],
    },
    "pm": {
        "frequent_tools": ["Read", "Write", "Edit"],
        "rare_tools": ["Glob", "Grep"],
        "skills_used": [],
        "skills_unused": [
            "impeccable",
            "baseline-ui",
            "fixing-accessibility",
            "fixing-metadata",
            "fixing-motion-performance",
            "emil-design-eng",
            "find-skills",
            "vercel-react-best-practices",
        ],
        "agents_used": [],
        "mcp_calls": {
            "mcp__claude_ai_Atlassian_Rovo": [
                "createJiraIssue",
                "getJiraIssue",
                "searchJiraIssuesUsingJql",
            ],
        },
        "mcp_unused": ["figma-dev", "chrome-devtools"],
        "file_exts": [".md", ".md", ".md", ".txt", ".md"],
        "intents": [
            "draft the Q3 roadmap document",
            "write the release notes for v2.4",
            "summarize the user research findings",
            "prepare the stakeholder update for next week",
            "outline the v3 PRD",
        ],
        "cite_phrases": [
            "Tagging the release as v2.4.0 per the release process.",
            "Adding the CHANGELOG.md entry alongside the API changes.",
            "Updating the architecture decision record (ADR).",
        ],
    },
}


def make_assistant_turn(profile: dict, turn_idx: int, ts: dt.datetime, rng: random.Random) -> dict:
    content: list[dict] = []

    if rng.random() < 0.45 and profile["cite_phrases"]:
        content.append({"type": "text", "text": rng.choice(profile["cite_phrases"])})

    roll = rng.random()
    tid = f"toolu_{turn_idx:04d}_{uuid.uuid4().hex[:8]}"

    if roll < 0.50:
        tool = rng.choice(profile["frequent_tools"])
        ext = rng.choice(profile["file_exts"])
        comp = rng.choice(["Button", "Modal", "Tooltip", "Sidebar", "Avatar", "Banner", "Drawer"])
        path = f"src/components/{comp}{turn_idx % 4}{ext}"
        content.append({
            "type": "tool_use",
            "name": tool,
            "id": tid,
            "input": {"file_path": path},
        })
    elif roll < 0.65 and profile["skills_used"]:
        content.append({
            "type": "tool_use",
            "name": "Skill",
            "id": tid,
            "input": {"skill": rng.choice(profile["skills_used"])},
        })
    elif roll < 0.75 and profile["agents_used"]:
        content.append({
            "type": "tool_use",
            "name": "Agent",
            "id": tid,
            "input": {
                "subagent_type": rng.choice(profile["agents_used"]),
                "description": "specialist task",
                "prompt": "...",
            },
        })
    elif roll < 0.92 and profile["mcp_calls"]:
        server = rng.choice(list(profile["mcp_calls"].keys()))
        op = rng.choice(profile["mcp_calls"][server])
        content.append({
            "type": "tool_use",
            "name": f"{server}__{op}",
            "id": tid,
            "input": {},
        })
    elif profile["rare_tools"]:
        content.append({
            "type": "tool_use",
            "name": rng.choice(profile["rare_tools"]),
            "id": tid,
            "input": {"query": "design tokens reference"},
        })
    else:
        content.append({
            "type": "tool_use",
            "name": "Read",
            "id": tid,
            "input": {"file_path": "README.md"},
        })

    return {
        "type": "assistant",
        "cwd": str(DEMO_DIR),
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "message": {
            "role": "assistant",
            "content": content,
            "usage": {
                "input_tokens": rng.randint(800, 3500),
                "cache_read_input_tokens": rng.randint(2000, 9000),
                "cache_creation_input_tokens": rng.randint(100, 1200),
                "output_tokens": rng.randint(100, 800),
            },
        },
    }


def synthesize_session(profile: dict, session_idx: int, days_ago: int, rng: random.Random) -> tuple[str, list[dict]]:
    sid = str(uuid.uuid4())
    base_ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=days_ago, hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
    )
    records: list[dict] = []

    intent = rng.choice(profile["intents"])
    records.append({
        "type": "user",
        "cwd": str(DEMO_DIR),
        "timestamp": base_ts.isoformat().replace("+00:00", "Z"),
        "message": {"role": "user", "content": [{"type": "text", "text": intent}]},
    })

    n_turns = rng.randint(6, 16)
    for i in range(n_turns):
        ts = base_ts + dt.timedelta(minutes=2 + i * rng.randint(2, 5))
        records.append(make_assistant_turn(profile, i, ts, rng))

    return sid, records


def ask_role() -> str:
    print("\nWhat role should the demo simulate?")
    print("  1. designer")
    print("  2. engineer")
    print("  3. pm")
    while True:
        ans = input("Pick [1/2/3]: ").strip().lower()
        if ans in ("1", "d", "designer"):
            return "designer"
        if ans in ("2", "e", "engineer", "eng"):
            return "engineer"
        if ans in ("3", "p", "pm"):
            return "pm"
        print("Please enter 1, 2, or 3.")


MEMORY_FILES = {
    "designer": {
        "user_role.md": (
            "---\nname: user-role\ndescription: Sam is a product designer on the Design Systems team.\nmetadata:\n  type: user\n---\n\n"
            "Sam is a product designer at Acme on the Design Systems team. They own the AITS component\n"
            "library's visual + interaction layer and are not a primary code contributor — they review and\n"
            "spec, but engineers implement. Frame suggestions around design decisions, specs, copy, and\n"
            "accessibility rather than code refactors.\n"
        ),
        "feedback_terse_summaries.md": (
            "---\nname: feedback-terse-summaries\ndescription: Sam wants terse headline-first summaries; expanded rationale only on request.\nmetadata:\n  type: feedback\n---\n\n"
            "Sam prefers terse summaries with the headline first, then the supporting detail. They get\n"
            "frustrated with long preambles or restating the question.\n\n"
            "**Why:** they triage many open threads per day and need to skim.\n"
            "**How to apply:** lead with the answer or the verdict. One sentence preamble max. Expand\n"
            "with bullets only if asked.\n"
        ),
        "feedback_never_push_develop.md": (
            "---\nname: feedback-never-push-develop\ndescription: Never run git push on the develop branch from Sam's session.\nmetadata:\n  type: feedback\n---\n\n"
            "Never run `git push` on the develop branch from Sam's session, regardless of context.\n\n"
            "**Why:** Sam is a designer and the develop branch is the integration branch for the engineering\n"
            "team. Pushes from a designer session caused merge conflicts and a rollback in February 2026.\n"
            "**How to apply:** if asked to push, refuse and tell Sam to flag the engineer who owns the\n"
            "in-flight code.\n"
        ),
        "project_modal_native_dialog.md": (
            "---\nname: project-modal-native-dialog\ndescription: The Modal component was rewritten to use the native dialog element in v1.4.\nmetadata:\n  type: project\n---\n\n"
            "The Modal component was rewritten in v1.4 (March 2026) to use the native HTML `<dialog>`\n"
            "element instead of a custom portal.\n\n"
            "**Why:** native dialog handles focus trap, escape-to-close, and backdrop click for free, and\n"
            "the legacy portal had a known Safari 16.4 focus bug.\n"
            "**How to apply:** when new overlays are proposed, copy the Modal pattern. Don't suggest a\n"
            "portal-based approach unless there's a specific reason native dialog won't work.\n"
        ),
        "project_token_rename.md": (
            "---\nname: project-token-rename\ndescription: Token names switched from --color-text/-background to --color-fg/-bg in v1.4.\nmetadata:\n  type: project\n---\n\n"
            "In v1.4 (April 2026) the design tokens were renamed:\n- `--color-text` → `--color-fg`\n- `--color-background` → `--color-bg`\n\n"
            "**Why:** consistency with the new short-form token convention. Old names are deprecated.\n"
            "**How to apply:** when reviewing or generating styles, prefer the new names. A codemod is\n"
            "scheduled for v1.5 to remove the old names entirely.\n"
        ),
        "reference_confluence_voice.md": (
            "---\nname: reference-confluence-voice\ndescription: Voice and tone guidelines live in the PDUX Confluence space; key page IDs noted.\nmetadata:\n  type: reference\n---\n\n"
            "Voice and tone guidelines live in the PDUX Confluence space.\n\n"
            "- Voice and tone overview: page 2226585643\n"
            "- Accessibility and inclusive language: page 2462744615\n"
            "- Button and link copy: page 2226585679\n"
            "- Error / warning / notification copy: page 2226585691\n\n"
            "Use these for any microcopy review or proposal.\n"
        ),
        # Stale entries auto-tune should flag
        "stale_q3_2025_tailwind.md": (
            "---\nname: stale-q3-2025-tailwind\ndescription: The Tailwind migration plan from Q3 2025 was abandoned; we kept CSS modules.\nmetadata:\n  type: project\n---\n\n"
            "Old note from Q3 2025: 'We're migrating to Tailwind starting next sprint.'\n\n"
            "Status: ABANDONED in Q4 2025. The team kept CSS modules. This memory is stale and should\n"
            "not influence current decisions — flagged for cleanup.\n"
        ),
        "stale_old_brand_voice.md": (
            "---\nname: stale-old-brand-voice\ndescription: The 2024 brand voice guidelines are superseded by the 2025 refresh.\nmetadata:\n  type: project\n---\n\n"
            "The 2024 'Brand voice v1' guidelines are no longer canonical.\n\n"
            "Replaced by the 2025 voice and tone refresh (linked from reference-confluence-voice).\n"
            "Status: STALE. Kept for archival only.\n"
        ),
    },
    "engineer": {
        "user_role.md": (
            "---\nname: user-role\ndescription: Sam is a frontend engineer on Platform Frontend, working in the AITS repo.\nmetadata:\n  type: user\n---\n\n"
            "Sam is a frontend engineer on the Platform Frontend team. They own implementation and tests\n"
            "for the AITS component library. They review designer-proposed specs and ship them.\n"
        ),
        "feedback_tests_required.md": (
            "---\nname: feedback-tests-required\ndescription: Every component change requires a co-located test file.\nmetadata:\n  type: feedback\n---\n\n"
            "Every component change requires a co-located `*.test.tsx` file. PRs without tests get rejected.\n\n"
            "**Why:** the team got burned in Q4 2025 by a regression that slipped through because the test\n"
            "file was 'too obvious to add.'\n"
            "**How to apply:** when modifying or adding components, always propose a corresponding test.\n"
        ),
        "project_storybook_8_migration.md": (
            "---\nname: project-storybook-8-migration\ndescription: All stories migrated to CSF3 in February 2026; drop CSF2 patterns when touched.\nmetadata:\n  type: project\n---\n\n"
            "Storybook 8 migration completed in February 2026. All stories use CSF3 now. When touching\n"
            "an old story file, migrate any remaining CSF2 patterns in the same PR.\n"
        ),
    },
    "pm": {
        "user_role.md": (
            "---\nname: user-role\ndescription: Sam is a PM on the AITS team responsible for roadmap + release planning.\nmetadata:\n  type: user\n---\n\n"
            "Sam is a product manager for AITS. They own the roadmap, the quarterly planning doc, and\n"
            "release-note coordination. They don't write code or design — they coordinate across\n"
            "Design Systems and Platform Frontend.\n"
        ),
        "project_q3_roadmap.md": (
            "---\nname: project-q3-roadmap\ndescription: Q3 2026 roadmap is in flight; spec doc lives in Confluence.\nmetadata:\n  type: project\n---\n\n"
            "Q3 2026 roadmap themes: accessibility sweep (Priya), empty-state audit (Sam), button hierarchy\n"
            "refresh (Dev). Spec docs in Confluence under 'AITS / Q3 Planning'.\n"
        ),
    },
}


def write_memory_files(role: str, proj_dir: Path) -> int:
    memory_dir = proj_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    # Wipe any pre-existing demo memory
    for f in memory_dir.glob("*.md"):
        f.unlink()

    entries = MEMORY_FILES.get(role, {})
    if entries:
        # Write or update the MEMORY.md index
        index_lines = ["# Memory index\n"]
        for fname, body in entries.items():
            (memory_dir / fname).write_text(body, encoding="utf-8")
            # First non-frontmatter line as the index hook
            hook_lines = body.split("\n---\n", 1)[-1].strip().split("\n")
            hook = next((l for l in hook_lines if l.strip() and not l.startswith("#")), "")[:120]
            index_lines.append(f"- [{fname[:-3]}]({fname}) — {hook}")
        (memory_dir / "MEMORY.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return len(entries)


def reset() -> None:
    flat = flatten_cwd(str(DEMO_DIR))
    proj_dir = PROJECTS_DIR / flat
    if not proj_dir.is_dir():
        print(f"Nothing to reset — {proj_dir} doesn't exist.")
        return
    n = 0
    for f in proj_dir.glob("*.jsonl"):
        f.unlink()
        n += 1
    print(f"Removed {n} synthetic transcript file(s) from {proj_dir}.")
    memory_dir = proj_dir / "memory"
    if memory_dir.is_dir():
        m = 0
        for f in memory_dir.glob("*.md"):
            f.unlink()
            m += 1
        try:
            memory_dir.rmdir()
        except OSError:
            pass
        print(f"Removed {m} memory file(s).")
    role_file = DEMO_DIR / ".claude" / ".role"
    if role_file.exists():
        role_file.unlink()
        print(f"Removed role file: {role_file}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", choices=["designer", "engineer", "pm"], help="Skip the interactive prompt")
    p.add_argument("--sessions", type=int, default=22, help="How many synthetic sessions to write (default 22)")
    p.add_argument("--seed", type=int, default=42, help="Random seed for reproducible demos")
    p.add_argument("--reset", action="store_true", help="Wipe synthetic transcripts and the .role file")
    args = p.parse_args(argv)

    if args.reset:
        reset()
        return 0

    role = args.role or ask_role()
    profile = ROLE_PROFILES[role]

    flat = flatten_cwd(str(DEMO_DIR))
    proj_dir = PROJECTS_DIR / flat
    proj_dir.mkdir(parents=True, exist_ok=True)

    for old in proj_dir.glob("*.jsonl"):
        old.unlink()

    rng = random.Random(args.seed)
    written = 0
    for i in range(args.sessions):
        days_ago = int(60 * i / args.sessions) + rng.randint(0, 2)
        sid, records = synthesize_session(profile, i, days_ago, rng)
        out = proj_dir / f"{sid}.jsonl"
        out.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        written += 1

    role_dir = DEMO_DIR / ".claude"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / ".role").write_text(role + "\n")

    n_memory = write_memory_files(role, proj_dir)

    print()
    print(f"Seeded — role: {role}")
    print(f"  {written} sessions in {proj_dir}")
    print(f"  {n_memory} memory file(s) in {proj_dir / 'memory'}")
    print(f"  Role file: {role_dir / '.role'}")
    print()
    print("Next:")
    print(f"  cd {DEMO_DIR}")
    print(f"  claude")
    print(f"  /auto-tune --cost-report")
    print()
    print(f"Reset: python3 {Path(__file__)} --reset")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
