---
name: auto-tune
description: Auto-customize this user's Claude Code setup (skills, MCP, CLAUDE.md, settings) for the current project and role by analyzing chat transcripts. Use when the user asks to "auto-tune", "shrink the system prompt", "tailor my setup for this folder", "optimize tokens", "prune unused skills", or `/auto-tune`. Always proposes before writing; never edits ~/.claude.json or memory.
---

# Auto-Tune

Tailors the user's Claude Code config to their **role** (designer / PM / engineer) and the **current project folder**, using their own chat history as evidence. Goal: shrink the per-turn system prompt by disabling unused skills/MCPs, tightening CLAUDE.md, and surfacing role-specific guardrails.

## When to invoke

Trigger on any of:
- The user types `/auto-tune` (with or without args).
- They ask to "shrink", "tighten", "optimize", "tailor", or "prune" their Claude Code setup.
- They ask why their system prompt is so large or which skills they actually use.

## Arguments

Parse from the user's message:
- `--global` — analyze every project folder, not just current.
- `--dry-run` — propose only, do not apply anything.
- `--role <designer|pm|engineer>` — explicit role override, persisted.
- `--apply` — skip the per-change confirmation and write everything (only when the user explicitly asks).
- `--discover` — also run the external-skill discovery step (Grok + GitHub + RSS + WebSearch). Implied by `--global`.
- `--corrections-only` — skip analyze/discover; only detect correction patterns and propose `tweak-skill` items.
- `--add-security-hook` — include the `add-hook` proposal that installs a `PreToolUse` URL-allowlist check.

Default invocation = analyze current folder, detect corrections, propose, confirm each change, apply approved subset. No discovery unless `--discover` or `--global`.

## How to run

The skill is a thin orchestrator. **All heavy work is done by Python scripts**; your job is to invoke them, present results, and collect approvals.

1. **Resolve the project folder.** Use `pwd`. If the user invoked from `~`, treat the run as a global one (`--global`).

2. **Detect role** by running:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/role.py detect --cwd <pwd> [--override <role>]
   ```
   Stdout is JSON: `{"role": "designer", "source": "memory|file|heuristic|override", "confidence": 0.0-1.0, "evidence": [...]}`.

   If `source == "heuristic"` and `confidence < 0.7`, **ask the user to confirm** via AskUserQuestion before proceeding. Then persist with:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/role.py set --scope <global|project> --role <role> --cwd <pwd>
   ```

3. **Analyze** transcripts:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/analyze.py --cwd <pwd> [--global] --out ~/.agents/skills/auto-tune/cache/signals.json
   ```

4. **Discover** (only if `--discover` or `--global`). Runs in three parts:
   a. Sync trusted authors from manual installs (cheap, idempotent):
      ```
      python3 ~/.agents/skills/auto-tune/scripts/trusted.py sync
      ```
      Reads `~/.agents/.skill-lock.json`, writes/refreshes `security/trusted_authors.txt`, `security/trusted_repos.txt`, and appends release feeds to `security/curators.txt`. Every author the user has manually installed becomes a trust seed for future discovery.
   b. Pure-HTTP providers (Python) — all free, no keys required:
      ```
      python3 ~/.agents/skills/auto-tune/scripts/discover.py --role <role> --providers github,rss,community [--dry-run]
      ```
      Output: `cache/candidates.json`. Default providers:
      - `github` — topic-tag search + `user:<trusted-author>+claude` targeted search.
      - `rss` — release feeds in `security/curators.txt`.
      - `community` — bundle of free community channels: `reddit` (r/ClaudeAI + r/LocalLLaMA), `hn` (Hacker News Algolia API), `awesome` (curated markdown lists in `security/aggregator_lists.txt`).

      All candidates are filtered by: spam-username heuristic (bot patterns drop to diagnostics), README must self-describe as a Claude skill (must contain `SKILL.md`/`claude code`/`frontmatter:` etc.), and trusted-repo dedupe. Trusted-author candidates bypass the heuristics (they've earned the pass). Curated sources (reddit/hn/awesome/rss) use a lower 0.15 relevance threshold; topic-tag search uses 0.3.

      **Optional paid provider** — Grok X-search (`grok` in `--providers`). Surfaces X threads with skill announcements that don't reach Reddit/HN. Requires `XAI_API_KEY` and costs ~$0.02/run. Skip by default; mention only if the user explicitly asks. If they want it, the steps are: create key at https://console.x.ai → `export XAI_API_KEY="xai-..."` in `~/.zshrc` → re-run with `--providers github,rss,community,grok`.
   c. WebSearch provider (you, the orchestrator):
      Run 2–3 WebSearch queries for `"claude code skill" <role> site:github.com` and similar. Pick the top 5 results that look relevant. For each, call `WebFetch` to pull the README, then run:
      ```
      python3 ~/.agents/skills/auto-tune/scripts/security.py scan-content <quarantined-path>
      ```
      to confirm the body is clean. Build a small JSON list `[{name, source_url, description, excerpt}]` and re-run `discover.py --providers web --web-results /tmp/web.json` to merge them in.
   Skip step 4 silently if the user didn't pass `--discover`/`--global`.

5. **Compose** (always, after analyze + discover). Pick the best skill per facet of the user's role, compute the token-budget delta, draft per-skill personalization from this user's transcripts/memory/corrections:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/compose.py --role <role> --cwd <pwd>
   ```
   Output: `cache/composition.json` containing facet coverage, bundle_actions (keep/enable/install/disable_in_folder), token_budget, and ready-to-append `## Project context (auto-tune)` blocks for each picked skill.

   The opinion map (`FACETS` constant in compose.py) is curated by hand — primary picks reflect editorial judgment. Edit the constant to change the picks. If a facet is `uncovered`, surface that to the user; the only honest fix is for them to find a new skill manually.

5b. **Subagent chain** (always, after compose). Generate the role's subagent chain — for designer that's orchestrator + 5 specialists (researcher / spec-writer / implementer / polish-reviewer / handoff). Each subagent's body is filled with: the user's actually-installed skills, the user's actually-installed MCPs (in `## Data sources` as "Connected now"), project memory rules, and recent correction patterns. Missing data-source MCPs (Pendo / Mixpanel / Dovetail / Mobbin) are listed as "manual paste — ask the user."
   ```
   python3 ~/.agents/skills/auto-tune/scripts/subagents.py --role <role> --cwd <pwd>
   ```
   Output: `cache/subagent_drafts.json` containing one entry per subagent (target path, body, rationale, missing-skill/MCP diagnostics).

   The chain map (`CHAINS` constant in subagents.py) is curated by hand per role. PM and engineer are scoffolded but their bodies are deferred to v5. If the user requests a role with no chain defined, surface "no chain for this role yet" cleanly.

6. **Detect corrections** (always, unless `--corrections-only` is set in which case skip 3 and 4):
   ```
   python3 ~/.agents/skills/auto-tune/scripts/corrections.py --out ~/.agents/skills/auto-tune/cache/corrections.json
   ```
   Then for each candidate in `corrections.json` whose `skill != "(global)"`, draft a 1–3 line additive constraint per [prompts/tweak_constraint.md](~/.agents/skills/auto-tune/prompts/tweak_constraint.md) and write it back into the file under the candidate's `proposed_edit` key. If the candidate's pattern doesn't yield a clean constraint, set `proposed_edit` to `null` and propose.py will skip it.

6. **Propose** changes:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/propose.py --signals ~/.agents/skills/auto-tune/cache/signals.json --role <role> --cwd <pwd> --out ~/.agents/skills/auto-tune/cache/proposal.json [--with-security-hook]
   ```

7. **Present** the proposal. Read `cache/proposal.json` and group by `type`:
   - `prune-skill` — skill symlinks to remove (global or per-project)
   - `prune-mcp` — MCP servers to disable per-project (`disabledMcpjsonServers`)
   - `gen-claude-md` — per-folder CLAUDE.md to create/update
   - `add-mcp` — MCP servers to install (show the exact `claude mcp add` command; do NOT run it)
   - `gen-skill` — net-new skill stubs (recurring workflows in transcripts)
   - `add-skill-external` — community skills already quarantined-clean, ready to promote
   - `recommend-agent-external` — community projects worth manual review (link only)
   - `tweak-skill` — additive constraint lines to append to an existing skill's SKILL.md
   - `personalize-skill` — append a `## Project context (auto-tune)` block to a picked skill
   - `compose-bundle` — read-only summary of the role's composed bundle
   - `gen-subagent` — write a generated subagent to `~/.claude/agents/<name>.md`
   - `add-hook` — security `PreToolUse` URL-allowlist hook in `~/.claude/settings.json`

   For each group, render a short summary + per-item rationale + estimated token savings. Keep it scannable; show the full `after` content only on request.

8. **Confirm**. Unless `--apply` was passed, use AskUserQuestion (multiSelect) to let the user pick which changes to approve per group. Default: nothing is applied without explicit selection.

9. **Apply** the approved subset:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/apply.py --proposal ~/.agents/skills/auto-tune/cache/proposal.json --approved <comma-separated-ids> [--dry-run]
   ```
   The script writes one line per change to `~/.agents/skills/auto-tune/cache/log.jsonl` for audit/undo.

10. **Report** back: applied count, skipped count, manual-action count, estimated total token savings, and the one-liner undo for each applied change (read from the log).

## Hard rules

- **Never edit `~/.claude.json`.** MCP additions are surfaced as commands for the user to run; the toolkit does not touch the global MCP server registry.
- **Never write to `~/.claude/projects/*/memory/`.** Memory is read-only signal.
- **Never auto-activate generated or discovered skills.** A `gen-skill` or `add-skill-external` proposal writes the source under `~/.agents/skills/<name>/SKILL.md` but does NOT create the symlink in `~/.claude/skills/`. Tell the user the one-line command to enable it.
- **Per-project disables go in `<project>/.claude/settings.local.json`**, not the global settings file.
- If a proposal would remove the user's last enabled skill of a kind they clearly use (per-role keyword), drop it from the proposal and log the suppression.
- **Never bypass the security gate.** Every URL that auto-tune itself fetches (discovery, RSS, GitHub API) MUST go through `security.py check-url` first. Every fetched body MUST land in `security/quarantine/` and pass `scan-content` before propose.py exposes it as `add-skill-external`. If the user asks you to fetch a URL outside the allowlist, refuse and explain — they should add the host to `security/allowlist.txt` first.
- **`tweak-skill` edits are additive-only.** Always append under "## Constraints (auto-tune)"; never modify existing skill text. If a proposed constraint contradicts existing text, drop the candidate and surface it for manual review.
- **Corrections with `skill == "(global)"` do not become `tweak-skill` proposals.** They are noise unless explicitly attributable; surface them as candidate CLAUDE.md additions for the relevant project folder instead.

## Output style

- Short, scannable summaries. Lead with counts and estimated savings, then list per-item.
- Never paste a full `signals.json` or `proposal.json` blob to the user.
- For diffs, show the smallest useful slice (added/removed lines), not the whole file.

## Failure modes

- If transcript parsing finds zero sessions: tell the user, ask whether to fall back to role-keyword defaults instead, and stop if they decline.
- If `~/.agents/.skill-lock.json` is missing or malformed: warn but proceed — registry edits are non-fatal.
- If a Python script exits non-zero: surface stderr verbatim, do not retry blindly.
