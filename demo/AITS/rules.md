# AITS — Extended team rules

This file extends `.claude/CLAUDE.md` with the longer-form team conventions
that don't fit cleanly in the canonical rule list. Claude is expected to
read this on every work session for context.

## Code review expectations

- All PRs require at least one review from a Design Systems team member AND one from Platform Frontend.
- Reviewer must verify the change works in light mode AND dark mode.
- Reviewer must verify the change works at 320px, 768px, and 1440px breakpoints.
- Reviewer must check Storybook entries are updated for any visual change.
- Reviewer must run the change locally before approving if it touches token files.
- "LGTM" without a substantive comment is not acceptable — explain what you checked.

## Branching and release cadence

- Long-lived feature branches must be rebased onto develop at least weekly.
- Release branches are cut from develop on the first Tuesday of each month.
- Hotfix branches go directly off main and require sign-off from the on-call.
- Cherry-picks into a release branch require an explanation in the PR description.
- The release branch freezes 48 hours before the scheduled publish.

## Testing policy

- Unit tests: vitest. Place alongside the component as `*.test.tsx`.
- Integration tests: real Postgres. Use the test-containers helper, not mocks.
- Visual regression: Percy on the nightly cron only. Not in PR checks.
- Accessibility tests: axe-core baseline on every PR via the lint job.
- Performance tests: Lighthouse CI on the public marketing site only.
- Snapshot tests are banned. Use explicit assertions for everything.

## Internationalization

- Every user-facing string must be wrapped in `t()` from `@acme/i18n`.
- Never concatenate translated strings — use ICU message format with placeholders.
- Pluralization must use the `{count, plural, ...}` ICU form, not if/else.
- Right-to-left support is mandatory for all new components.
- Date formatting goes through `@acme/i18n/date`, never `toLocaleDateString` directly.

## Performance budgets

- Component bundle size: no single component over 8 KB minzipped.
- Initial route bundle (admin console): under 180 KB minzipped.
- LCP target: under 2.0s on the public marketing site.
- CLS target: under 0.05 on every page.
- INP target: under 200ms p75.

## Data sources Claude can use

- Confluence pages under the "Acme Design Standards" space.
- Figma files under the "AITS / Components" project.
- The team's Notion knowledge base (read-only — read, don't write).
- The internal Pendo dashboards for usage metrics (manual paste only).

## Things to never do

- Don't suggest installing new top-level dependencies without explicit team approval.
- Don't modify `tokens.css` without an ADR in `docs/adr/`.
- Don't add `useEffect` that runs on every render without a dependency array.
- Don't bypass the design-token system with inline pixel values.
- Don't use `dangerouslySetInnerHTML` without DOMPurify-sanitized content.

## Communication

- Big decisions go in `memory.md` so future contributors (and future Claude) have context.
- ADRs live at `docs/adr/NNN-title.md` and follow the lightweight ADR format.
- The team's Slack channel for AITS questions is `#acme-aits`.
- Async updates land in the weekly `#design-systems-weekly` thread.
