# auto-tune

A Claude Code skill that **auto-customizes your setup** — prunes unused skills/MCPs, generates per-folder `CLAUDE.md`, discovers role-relevant community skills, composes them into a coherent multi-facet bundle, and personalizes each skill with project-specific context drawn from your transcripts and memory.

Built by [Ritwik Bisht](https://github.com/ritwik-bisht) iteratively with Claude Code. The goal: shrink the per-turn system-prompt tax (fewer enabled skills + tighter `CLAUDE.md` + role-specific personalization) without losing capability.

## What it does

- **Prune** — disable skills/MCPs unused in the last 90 days, per-folder, reversibly.
- **Tighten `CLAUDE.md`** — generate a folder-specific one from your transcripts, memory, role, and observed file extensions.
- **Discover** — find role-relevant community skills via GitHub, RSS feeds, Reddit, Hacker News, awesome-claude-code lists, and (optionally) Grok X-search.
- **Compose** — for a chosen role (designer / pm / engineer), pick the best skill per facet (research, spec, implementation, a11y, motion, polish, metadata for designer) from your installed + discovered pool. Compute the token-budget delta.
- **Personalize** — append a `## Project context (auto-tune)` block to each picked skill's `SKILL.md` derived from your project's signals + memory + correction history. Upstream content untouched.
- **Self-tune** — detect repeated user-correction patterns in transcripts and propose additive constraint lines for the implicated skill.
- **Gate** — every internet fetch passes through a host allowlist + content scanner. Downloads are quarantined and scanned for prompt-injection markers, shell-pipe-installs, credential paths, exfil callbacks, large base64 blobs, etc. before promotion.

## What it does NOT do

- It does **not** evaluate skill output quality. Picks are based on a hand-curated opinion map + role-relevance keywords + trusted-author boosts; you're still the final judge.
- It does **not** generate persistent subagents (deferred — needs different machinery).
- It does **not** edit `~/.claude.json` (MCP additions are surfaced as commands for you to run).
- It does **not** modify your memory files (`~/.claude/projects/*/memory/*.md`) — those are read-only signal.
- It does **not** auto-enable discovered skills. New skill sources land at `~/.agents/skills/<name>/` but require a manual `ln -s` to activate.

## Install

This repo is **private** — you need to be a collaborator on `ritwik-bisht/auto-tune` for the clone to succeed. The `find-skills` install flow does not work with private repos; use the manual path below.

```bash
git clone git@github.com:ritwik-bisht/auto-tune.git ~/.agents/skills/auto-tune-src
ln -s ~/.agents/skills/auto-tune-src/skills/auto-tune ~/.agents/skills/auto-tune
ln -s ~/.agents/skills/auto-tune ~/.claude/skills/auto-tune
```

Then add an entry to `~/.agents/.skill-lock.json` so Claude Code lists it as available. Minimal shape:

```json
"auto-tune": {
  "source": "ritwik-bisht/auto-tune",
  "sourceType": "github-private",
  "sourceUrl": "git@github.com:ritwik-bisht/auto-tune.git",
  "skillPath": "skills/auto-tune/SKILL.md",
  "installedAt": "2026-05-19T00:00:00.000Z",
  "updatedAt": "2026-05-19T00:00:00.000Z"
}
```

Restart your Claude Code session after editing skill-lock.json so the new skill is picked up.

## Use

From inside any project folder in Claude Code:

```
/auto-tune --dry-run
```

The orchestrator (Claude reading `SKILL.md`) runs the pipeline: analyze → discover → compose → corrections → propose. Output is a list of proposed changes you approve per-item. Nothing writes to disk without `--apply` or explicit per-item approval.

### Flags

- `--global` — scan every project folder, not just current.
- `--dry-run` — propose only.
- `--role <designer|pm|engineer>` — set/override role; persisted in `<project>/.claude/.role`.
- `--discover` — include the community-discovery step.
- `--corrections-only` — skip analyze/discover; only detect tweak candidates.
- `--add-security-hook` — propose a `PreToolUse` URL-allowlist hook that extends gating to all Claude Code internet fetches.

### Roles

The `FACETS` constant in [`skills/auto-tune/scripts/compose.py`](skills/auto-tune/scripts/compose.py) defines the opinion map per role. Edit it to change picks for your team.

## Undo

Every applied change writes a line to `~/.agents/skills/auto-tune/cache/log.jsonl` with the literal undo command. Examples:

- Prunes → edit `<project>/.claude/settings.local.json` to remove the entry, or `rm` the whole file.
- Generated `CLAUDE.md` → `rm <project>/.claude/CLAUDE.md` (or `mv` from `.autotune.bak` if there was an existing one).
- Personalized skill → `mv ~/.agents/skills/<name>/SKILL.md.autotune.bak ~/.agents/skills/<name>/SKILL.md`.

Full rollback:

```bash
find ~/.agents/skills ~/.claude -name '*.autotune.bak' -exec sh -c 'mv "$1" "${1%.autotune.bak}"' _ {} \;
rm -rf ~/.agents/skills/auto-tune/cache ~/.agents/skills/auto-tune/security/quarantine/*
```

## Security stance

Hosts auto-tune may fetch from are listed in [`skills/auto-tune/security/allowlist.txt`](skills/auto-tune/security/allowlist.txt). Edit it before enabling — anything not listed is refused.

Content scanning rules live in [`skills/auto-tune/security/patterns.json`](skills/auto-tune/security/patterns.json). Defaults cover prompt-injection markers, shell-pipe installs, credential paths, exfil callbacks, raw-IP URLs, large base64 blobs.

Quarantine: every fetched body lands in `security/quarantine/<sha256>/` and is scanned before any apply step can promote it.

## Optional: Grok X-search

The Grok provider is wired but disabled by default — it costs ~$0.02/run via the xAI API. To enable:

```bash
# get a key at https://console.x.ai
export XAI_API_KEY="xai-..."
```

Then run with `--providers github,rss,community,grok`. The prompt template is at [`skills/auto-tune/prompts/grok_xsearch.md`](skills/auto-tune/prompts/grok_xsearch.md) — tune it for your role.

## Layout

```
skills/auto-tune/
├── SKILL.md                  # orchestrator instructions (Claude reads this)
├── scripts/                  # Python helpers — heavy lifting
├── prompts/                  # editable templates (CLAUDE.md, skill stub, Grok prompt)
├── security/
│   ├── allowlist.txt
│   ├── patterns.json
│   ├── curators.txt
│   ├── aggregator_lists.txt
│   ├── whitelisted_authors.txt
│   └── quarantine/           # gitignored
└── cache/                    # gitignored
```

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

This was built iteratively in conversation with Claude Code (Opus 4.7). Seed skills that informed the designer facet map: [`emilkowalski/skill`](https://github.com/emilkowalski/skill), [`pbakaus/impeccable`](https://github.com/pbakaus/impeccable), [`ibelick/ui-skills`](https://github.com/ibelick/ui-skills), [`vercel-labs/skills`](https://github.com/vercel-labs/skills), [`vercel-labs/agent-skills`](https://github.com/vercel-labs/agent-skills).
