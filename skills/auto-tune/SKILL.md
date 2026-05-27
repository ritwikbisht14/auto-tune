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
- `--refresh` — also run `refresh.py` to look for higher-quality community alternatives to installed skills. Emits `swap-skill` items. Opt-in; recommended weekly cadence.
- `--refresh-content` — re-fetch the designer-content Confluence catalog from the folder defined in `config/designer-content.json` and update that config with any newly-discovered pages. Run when new pages are added to your UX copy folder.
- `--branch-isolate` — append a fenced block to `<project>/.gitignore` so per-project auto-tune writes (`CLAUDE.md`, `settings.local.json`, `agents/`) stay in the user's tree only and don't merge into team branches. Idempotent.
- `--max-per-facet N` (v5.3) — cap external-skill candidates surfaced per facet. Default 3. Pass `--show-all` to disable the cap.
- `--show-all` (v5.3) — include every external candidate, not just the top per facet. Useful when you want to audit what discovery dropped.
- `--track-rejections` (passed to apply.py, v5.3) — log unapproved `add-skill-external` / `swap-skill` items as rejections in `cache/feedback_history.json` so the next discovery downweights them.
- `--cost-report` (v6) — run only the read-only token-cost measurement step (step 11). Skips analyze/discover/compose/subagents/propose/apply. Output: `cache/cost_report.json` + a scannable summary in chat.
- `--full` (v6) — run every step including the cost report at the end. Equivalent to default + `--discover` + `--refresh` + step 11.

Default invocation = analyze current folder, detect corrections, propose, confirm each change, apply approved subset. No discovery unless `--discover` or `--global`. No cost report unless `--cost-report` or `--full`.

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

6a. **Drift detect** (always — runs on every /auto-tune):
   ```
   python3 ~/.agents/skills/auto-tune/scripts/drift.py --cwd <pwd>
   ```
   Walks `cache/log.jsonl` for `prune-skill` entries in the last 60 days and counts keyword overlap with user messages since each prune. If a pruned skill's topic keeps appearing, surfaces a `restore-skill` candidate. Silent if no candidates.

6b. **Upgrade detect** (optional — only when `--refresh` is set):
   ```
   python3 ~/.agents/skills/auto-tune/scripts/refresh.py --role <role> --cwd <pwd>
   ```
   For each facet in the user's role with an installed primary, finds candidates in `cache/candidates.json` that score higher than the installed baseline by ≥0.15. Emits `swap-skill` candidates to `cache/upgrades.json`. Requires `discover.py` to have run first (so candidates are available).

6c. **Confluence catalog refresh** (optional — only when `--refresh-content` is set, and only when role=designer):
   - Pre-flight: ensure Atlassian MCP is connected. If not, halt with the same auth message as the designer-content pre-flight (step 9).
   - Read `config/designer-content.json` to get the user's `cloud_id` and `folder_id`. If the file doesn't exist, halt with: *"designer-content is not yet configured. Copy `config/designer-content.json.example` to `config/designer-content.json` and fill in your Confluence cloud_id, folder_id, and page IDs first."*
   - Run CQL: `parent = <folder_id> AND type = page` in `cloudId <cloud_id>`.
   - For each result, capture `id`, `title`, and any `summary`/excerpt. Match the title against the user's existing `conditional_pages` entries in the config to decide whether the page is a new one or a known one whose title changed.
   - Propose updates to `config/designer-content.json` (not the template) as a `tweak-skill`-equivalent item. Once applied, the next `subagents.py` run will regenerate the catalog block in [content.md.tmpl](~/.agents/skills/auto-tune/prompts/subagent_templates/designer/content.md.tmpl) automatically.

6. **Propose** changes:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/propose.py --signals ~/.agents/skills/auto-tune/cache/signals.json --role <role> --cwd <pwd> --out ~/.agents/skills/auto-tune/cache/proposal.json [--with-security-hook]
   ```

7. **Present** the proposal. Read `cache/proposal.json` and group by `type`:
   - `prune-skill` — skill symlinks to remove (global or per-project)
   - `prune-mcp` — MCP servers to disable per-project (`disabledMcpjsonServers`)
   - `gen-claude-md` — per-folder CLAUDE.md to create OR to overwrite an existing auto-tune-generated one (the file must carry the `<!-- auto-tune-generated -->` marker; otherwise an `append-claude-md` is emitted instead)
   - `append-claude-md` (v5.1) — additive appendix to a team-authored CLAUDE.md; preserves existing content verbatim and only adds a fenced auto-tune addendum at the end
   - `add-mcp` — MCP servers to install (show the exact `claude mcp add` command; do NOT run it)
   - `gen-skill` — net-new skill stubs (recurring workflows in transcripts)
   - `add-skill-external` — community skills already quarantined-clean, ready to promote
   - `recommend-agent-external` — community projects worth manual review (link only)
   - `tweak-skill` — additive constraint lines to append to an existing skill's SKILL.md
   - `personalize-skill` — append a `## Project context (auto-tune)` block to a picked skill
   - `compose-bundle` — read-only summary of the role's composed bundle
   - `gen-subagent` — write a generated subagent to `~/.claude/agents/<name>.md`
   - `add-hook` — security `PreToolUse` URL-allowlist hook in `~/.claude/settings.json`
   - `swap-skill` (v5) — replace an installed skill with a higher-quality community alternative (requires `--refresh`)
   - `restore-skill` (v5) — re-enable a previously-pruned skill that the user keeps asking about
   - `branch-isolate` (v5) — append fenced block to `<project>/.gitignore` (requires `--branch-isolate`)
   - `manual-find-skill` (v5) — surface a skill gap for the user to find externally; paste the URL back to /auto-tune to wire it in
   - `discovery-summary` (v5.3) — read-only info item shown at the top of the discovery section; lists how many candidates were filtered (spam-username, README-thin, created-pushed gap, etc.) and how many were capped by the per-facet limit. Helps the user sanity-check that good candidates weren't being dropped.

   **Render the proposal using this exact four-section format** (the "AITS report format" — same on the proposal screen, the cost-report screen, and the final apply screen). Keep it scannable; the audience reads top-to-bottom.

   ```
   # Auto-tune — {role} setup detected

   ## Token savings
   - Loaded per turn (before): ~<n> tokens
   - Loaded per turn (after applying all proposals): ~<n> tokens
   - Estimated savings: ~<delta> tokens per turn
   - Projected at ~50 messages/day: ~<n> tokens/day saved

   ## Subagent chain ({role})
   - <name> (<model>) — <one-line role>
   - <name> (<model>) — <one-line role>
   ...

   ## Recommendations
   **Prune skills (<n>)**
   - <skill-name> — <one-line reason>
   ...

   **Disable MCPs (<n>)**
   - <mcp-name> — <one-line reason; include `tools_used_60d` count>
   ...

   **CLAUDE.md cleanup (<n>)**
   - <path> — <X of Y rules uncited; propose tightened version>
   ...

   **Memory hygiene (<n>)**
   - <memory-file> — <last referenced N days ago / superseded by …>
   ...

   **Other (<n>)**
   - <tweak-skill / restore-skill / add-skill-external / gen-subagent / etc.> — <one-line reason>
   ...

   ## Summary
   <2–3 sentences. State the role detected, the headline savings number, and what's about to be proposed for approval. End with a one-line "approve which to apply?" lead-in.>
   ```

   Item-type → recommendation-category mapping (use this when building the Recommendations section):
   - `prune-skill` → **Prune skills**
   - `prune-mcp` → **Disable MCPs**
   - `gen-claude-md` / `append-claude-md` → **CLAUDE.md cleanup**
   - Memory-file flags (from `cache/cost_report.json` `type: "memory"` with stale signals) → **Memory hygiene**
   - Everything else (`gen-skill`, `add-skill-external`, `recommend-agent-external`, `tweak-skill`, `personalize-skill`, `gen-subagent`, `swap-skill`, `restore-skill`, `add-mcp`, `add-hook`, `branch-isolate`, `manual-find-skill`, `discovery-summary`) → **Other**

   For the **Subagent chain** section: pull from `cache/subagent_drafts.json` (after step 5b ran) and list each subagent with its model + a 6–12 word role description. If `subagent_drafts.json` is missing (e.g. `--cost-report` standalone with no prior compose), instead read `~/.claude/agents/*.md` for the user's currently-installed chain and list those.

   Show the full `after` content of any single proposal only when the user explicitly asks for it ("show me the new CLAUDE.md", "what would the swap look like"). Don't paste full diffs by default.

8. **Confirm**. Unless `--apply` was passed, use AskUserQuestion (multiSelect) to let the user pick which changes to approve per group. Default: nothing is applied without explicit selection.

9. **Apply** the approved subset:
   ```
   python3 ~/.agents/skills/auto-tune/scripts/apply.py --proposal ~/.agents/skills/auto-tune/cache/proposal.json --approved <comma-separated-ids> [--dry-run]
   ```
   The script writes one line per change to `~/.agents/skills/auto-tune/cache/log.jsonl` for audit/undo.

10. **Report** back using the same AITS report format from step 7, but with applied results filled in:

    ```
    # Auto-tune — applied

    ## Token savings
    - Loaded per turn (before): ~<n> tokens
    - Loaded per turn (now): ~<n> tokens
    - Saved: ~<delta> tokens per turn
    - Projected at ~50 messages/day: ~<n> tokens/day saved

    ## Subagent chain ({role})
    <same list as step 7; note which were newly created vs already-installed>

    ## Recommendations applied
    **Pruned skills (<n>)** — <names>
    **Disabled MCPs (<n>)** — <names>
    **CLAUDE.md cleanup** — <path> tightened (<X rules dropped>)
    **Memory hygiene** — <n> files removed
    **Other** — <n applied> (<names>)

    ## Summary
    <2 sentences. Total applied count, total skipped count, headline savings.
    End with: "Undo any change with the matching line from
    `~/.agents/skills/auto-tune/cache/log.jsonl`.">
    ```

    For the cost-report step (step 11) standalone, use the same format but
    omit "Recommendations" and replace it with "Top offenders" — the
    same one-line-per-item structure, just labeled differently to signal
    "diagnostic, not proposal."

11. **Cost report** (only when `--cost-report` or `--full` is set; v6):
    ```
    python3 ~/.agents/skills/auto-tune/scripts/measure.py --cwd <pwd> --signals ~/.agents/skills/auto-tune/cache/signals.json --out ~/.agents/skills/auto-tune/cache/cost_report.json
    ```
    Read `cache/cost_report.json` back and render a compact summary in chat:
    - **Lead with the totals**: per-turn loaded bytes/tokens, subagent-chain bytes when invoked.
    - **Top 5 offenders** by per-turn token cost, each with the one-line `user_actions_to_consider` rationale.
    - For each item, do NOT paste full bodies — show name, type, per-turn or per-invocation cost, and the top action.
    - Always end with: *"Full breakdown: `~/.agents/skills/auto-tune/cache/cost_report.json`. Read-only diagnostics — nothing has been changed. Apply suggestions manually when you're ready."*

    **This step emits no proposals and applies no edits.** It is read-only diagnostics. The user reads the report and decides what to trim manually (deleting a skill symlink, disabling an MCP server, editing a CLAUDE.md, trimming a subagent's `tools:` allowlist). When `--cost-report` is set standalone, skip steps 3–10 entirely; the cost report uses `cache/signals.json` from the most recent analyze run.

    If `cache/signals.json` is missing when `--cost-report` is invoked standalone, run step 3 (analyze) first, then proceed to step 11.

## Hard rules

- **Never edit `~/.claude.json`.** MCP additions are surfaced as commands for the user to run; the toolkit does not touch the global MCP server registry.
- **Never write to `~/.claude/projects/*/memory/`.** Memory is read-only signal.
- **Never auto-activate generated or discovered skills.** A `gen-skill` or `add-skill-external` proposal writes the source under `~/.agents/skills/<name>/SKILL.md` but does NOT create the symlink in `~/.claude/skills/`. Tell the user the one-line command to enable it.
- **Per-project disables go in `<project>/.claude/settings.local.json`**, not the global settings file.
- If a proposal would remove the user's last enabled skill of a kind they clearly use (per-role keyword), drop it from the proposal and log the suppression.
- **Never bypass the security gate.** Every URL that auto-tune itself fetches (discovery, RSS, GitHub API) MUST go through `security.py check-url` first. Every fetched body MUST land in `security/quarantine/` and pass `scan-content` before propose.py exposes it as `add-skill-external`. If the user asks you to fetch a URL outside the allowlist, refuse and explain — they should add the host to `security/allowlist.txt` first.
- **`tweak-skill` edits are additive-only.** Always append under "## Constraints (auto-tune)"; never modify existing skill text. If a proposed constraint contradicts existing text, drop the candidate and surface it for manual review.
- **Corrections with `skill == "(global)"` do not become `tweak-skill` proposals.** They are noise unless explicitly attributable; surface them as candidate CLAUDE.md additions for the relevant project folder instead.
- **Team-authored CLAUDE.md is sacred (v5.1).** Every CLAUDE.md that auto-tune writes carries the `<!-- auto-tune-generated -->` HTML marker at the top. Before proposing `gen-claude-md` for an existing file, `propose_claude_md` checks for that marker. If it's missing, the file is treated as team-authored and **never overwritten** — propose.py emits `append-claude-md` (additive appendix only) instead. apply.py enforces the same rule as a second line of defense: `apply_gen_claude_md` refuses to overwrite a marker-less file even if a stale `gen-claude-md` item reaches it. The headline failure mode this prevents: deleting team conventions on a shared machine just because the current user's role doesn't trigger them often.
- **MCP detection (v5):** subagents.py and apply.py read `~/.agents/skills/auto-tune/security/connected_mcps.txt` to discover claude.ai-managed MCPs (e.g. `claude_ai_Atlassian_Rovo`). The auth cache at `~/.claude/mcp-needs-auth-cache.json` only lists MCPs that *need* auth — once authenticated, an MCP disappears from that cache, so the connected_mcps.txt file is the source of truth for "authenticated and ready." `MCP_ALIASES` in subagents.py maps the logical name `atlassian` to either `atlassian` (self-hosted) or `claude_ai_Atlassian_Rovo` (claude.ai-managed).
- **Atlassian onboarding (v5):** designer-content cannot generate without an Atlassian MCP because it reads the user's UX copy guidelines on every invocation. When `gen-subagent:designer-content` is proposed and the user has no Atlassian MCP connected, apply.py refuses with: *"Run /mcp and authenticate Atlassian Rovo (or install a self-hosted atlassian MCP), then add the MCP name to `~/.agents/skills/auto-tune/security/connected_mcps.txt` and re-run /auto-tune."* The orchestrator surfaces this *before* asking for confirmations, so the user fixes it first.
- **designer-content config:** the specific Confluence cloud_id, folder_id, and page IDs are user-specific and live in `config/designer-content.json` (gitignored). The shipped template in `prompts/subagent_templates/designer/content.md.tmpl` carries only placeholders; `subagents.py` substitutes values from the local config at generation time. If the config file is missing, the rendered subagent body includes a "configure me" notice instead of any specific values — nothing private leaks.
- **designer-researcher Slack mode (v5.2):** researcher reads Slack channels via the `claude_ai_Slack` MCP, extracts UX pain points, writes a structured doc to `<cwd>/docs/feedback/<channel>-<date>.md`, walks the user through approve/skip/defer per item, then returns a structured payload to `designer-fullstack` for auto-routing. Strictly read-only (never posts, reacts, or DMs). Per-channel cursors at `cache/slack_cursors/<channel_id>.json` enable incremental scans — each invocation processes only new messages since the last successful scan. Cursor advances only on success; partial failure leaves it untouched so retry reprocesses.
- **External skill finder (v5.3):** discovery's noise-reduction layer is in `discover.py`'s hard filters (skill-substance via deep-inspect, created-pushed gap, file-count floor, tightened spam-username and README-substance checks) and soft filters in `quality_score` (cross-provider corroboration boost, fork-ratio penalty, active-maintenance bonus, issue-engagement bonus, feedback-history adjustment, structure-score contribution). Editorial picks live in `security/curated_seeds.json` (committed); the user's install/reject history lives in `cache/feedback_history.json` (gitignored). `propose.py` groups external candidates by facet and caps at 3 per facet by default (override with `--max-per-facet N` or `--show-all`).
- **Cost report (v6) is read-only.** `measure.py` produces `cache/cost_report.json` with per-item token cost estimates and `user_actions_to_consider` strings. It emits NO proposals, takes NO approvals, and writes nothing outside `cache/`. The user reads the report and trims manually. Per-MCP token estimates come from the hand-maintained `security/mcp_tool_counts.json` reference table (user-editable). Per-skill and per-subagent measurements read the actual file bytes; CLAUDE.md rule-citation tracking uses bag-of-words keyword matching against assistant turns over the configured window (default 60 days).

## Output style

- Short, scannable summaries. Lead with counts and estimated savings, then list per-item.
- Never paste a full `signals.json` or `proposal.json` blob to the user.
- For diffs, show the smallest useful slice (added/removed lines), not the whole file.

## Failure modes

- If transcript parsing finds zero sessions: tell the user, ask whether to fall back to role-keyword defaults instead, and stop if they decline.
- If `~/.agents/.skill-lock.json` is missing or malformed: warn but proceed — registry edits are non-fatal.
- If a Python script exits non-zero: surface stderr verbatim, do not retry blindly.
