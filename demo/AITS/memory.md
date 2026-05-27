# AITS — Project memory

The team's accumulated context for working in this repo. Claude reads this
every session so it remembers what we've already decided and what's in flight.
Updated after every quarterly planning meeting.

## Recent decisions

- **v1.4 token rename (2026-04-02).** Renamed `--color-text` → `--color-fg` and `--color-background` → `--color-bg` to match the new short-form token convention. Any code referencing the old names is deprecated; codemod scheduled for v1.5.
- **Dialog API switch (2026-03-18).** Replaced the custom Modal portal with the native `<dialog>` element. The Modal component is the canonical example; copy the pattern when introducing new overlays.
- **Storybook 8 upgrade (2026-02-10).** Migrated all stories to CSF3. Drop any remaining CSF2 patterns when touching old stories.
- **Tone re-write (2026-01-22).** UX copy moved from "we" to "you"-framing across the admin console. Match this when proposing new microcopy.

## Active workstreams

- **Empty-state pattern audit (Sam — designer).** Reviewing every empty state for consistency with the new illustration system. Spec doc lives at `docs/empty-state-audit.md`.
- **Accessibility sweep (Priya — designer).** Per-component a11y review; goal is WCAG AA across the suite by end of Q2.
- **Button hierarchy refresh (Dev — designer-engineer collab).** Reducing button variants from 7 → 4 across the admin console. Spec is approved; implementation begins next sprint.

## Known issues (don't waste time rediscovering)

- The Modal component does NOT trap focus correctly in Safari 16.4 — a polyfill ships with v1.4.3.
- `Tooltip` placement is broken inside scroll containers; tracked as AITS-412.
- The `useMediaQuery` hook re-renders on every scroll on iOS Safari; AITS-389.
- Storybook controls do not persist across HMR; restart Storybook if controls disappear.

## Team conventions Claude should respect

- Sam (the user this session) is a designer, not a primary code contributor. Frame suggestions around design + spec output, not implementation deep-dives.
- Sam prefers terse summaries with the headline first; expanded rationale only on request.
- Sam does NOT use Chrome DevTools-style debugging — defer those workflows to engineering.
- Sam DOES use Figma + Confluence daily; lean on those integrations.

## Stale context (kept for archival; not currently active)

- The Tailwind migration plan from Q3 2025 — abandoned; we kept CSS modules.
- The "Component DNA" doc from Q4 2024 — superseded by the token system.
- The old "Brand voice v1" guidelines — replaced by the 2025 voice and tone refresh.
