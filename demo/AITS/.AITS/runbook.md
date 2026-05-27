# AITS — operator runbook (NOT for the audience)

This folder pretends to be **AITS — the Acme Internal Tooling Suite**, a
real-looking React component repo. Everything outside `.AITS/` is the staged
"project." Everything inside `.AITS/` is operator machinery: synthesized
history seeder, reset script, this runbook.

The parent `README.md`, `.claude/CLAUDE.md`, `rules.md`, and `memory.md` all
deliberately read like real internal documentation — no mention of "demo" or
"auto-tune" — so when Claude Code starts up here, it behaves as if it's
working in a real production codebase rather than narrating a pitch.

---

## Seed it

```bash
python3 ~/AITS/.AITS/setup.py            # interactive role prompt
python3 ~/AITS/.AITS/setup.py --role designer
python3 ~/AITS/.AITS/setup.py --role engineer
```

This generates:
- 22 synthetic JSONL transcripts in `~/.claude/projects/-Users-<you>-AITS/`
- Several memory files in `~/.claude/projects/-Users-<you>-AITS/memory/`
  (some intentionally stale — auto-tune flags them)
- `.claude/.role` in the AITS folder

## Run it for the audience

```bash
cd ~/AITS
claude
/auto-tune --cost-report     # the headline: token cost diagnostics
/auto-tune                   # full flow: prune proposals
```

## Reset between runs

```bash
python3 ~/AITS/.AITS/setup.py --reset
```

## The role flip (the kicker)

After running the designer version, re-seed as engineer:

```bash
python3 ~/AITS/.AITS/setup.py --role engineer
```

Re-run `/auto-tune --cost-report` and the proposal flips: design skills are
now flagged, Chrome DevTools is kept, Figma is flagged.

---

## What's loaded every turn in this demo

Bigger "before" number = more dramatic savings number. The demo deliberately
piles things into the per-turn context:

| Source | Approx tokens / turn | What's in it |
|---|---|---|
| `.claude/CLAUDE.md` | ~1,000 | 40+ rules across design, frontend, infra, release, security, observability |
| `rules.md` | ~700 | Extended team conventions (review, releases, i18n, perf, data sources) |
| `memory.md` | ~700 | Recent decisions, active workstreams, known issues, stale context |
| `.claude/settings.local.json` | ~500 | Permissions allowlist + env vars |
| Memory files (~6) | ~1,200 | Per-project memories, including 2 stale ones for auto-tune to flag |
| Auto-tune SKILL.md | ~5,000 | The skill itself |
| Other installed skills | ~700 | Frontmatter of 8 other skills |
| MCP tool schemas | ~5,500 | chrome-devtools (~4,500) + figma-dev (~1,000) |
| **Total** | **~15,000+** | |

After pruning (designer):
- chrome-devtools disabled → -4,500
- 5 unused skills pruned → -500
- CLAUDE.md tightened (24 rules dropped) → -700
- 2 stale memory files removed → -400
- **Savings: ~6,000+ tokens per turn**

---

## Auto-tune output format (v6+)

The auto-tune SKILL.md now prescribes a strict report format for the audience:

1. **Token savings** — before / after / per-turn delta
2. **Subagent chain** — the role's specialist agents with their roles
3. **Recommendations** — grouped categories with per-item rationale
4. **Summary** — 2–3 sentences

This is what the audience sees on screen. The full proposal JSON is still
written to `cache/proposal.json` for reference.

---

## Safety

Seeder writes only to:
- `~/.claude/projects/-Users-<you>-AITS/*.jsonl` (transcripts)
- `~/.claude/projects/-Users-<you>-AITS/memory/*.md` (memories)
- `~/AITS/.claude/.role` (role choice)

`.AITS/setup.py --reset` removes all three. Nothing else is touched.
