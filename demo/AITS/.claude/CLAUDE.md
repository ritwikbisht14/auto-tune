# AITS — Claude rules

This is the canonical guidance for Claude when working inside the AITS
(Acme Internal Tooling Suite) repo. Cover everything: design, frontend,
backend, infra, release process. Some rules apply only to specific roles —
Claude is expected to consult them anyway.

**Also read at the start of every session:**
- `rules.md` at the repo root — extended team conventions (review, releases, i18n, perf).
- `memory.md` at the repo root — accumulated decisions, active workstreams, known issues.

## Design conventions

- All new components must follow the existing button hierarchy (primary, secondary, ghost, destructive)
- Always use the design tokens from `src/styles/tokens.css` for spacing — do not hardcode pixel values
- Contrast ratios must meet WCAG AA standards (4.5:1 for normal text, 3:1 for large text)
- Add proper aria-labels to all interactive elements; never rely on icon-only labels
- Match copy tone to the company voice and tone guidelines (warm, plain, never condescending)
- Component variants must be data-attribute-driven, not class-name-driven, for theming flexibility
- Empty states must use the new illustration system; never ship "no data" text without an illustration
- Focus rings are tokenized in `--ring-*`; never roll your own focus styles

## Frontend engineering

- Prefer functional components and hooks over class components
- Co-locate component-specific CSS in a sibling `.css` file with the same basename
- Type all props explicitly with TypeScript interfaces, not inline object types
- Test components with vitest; place tests alongside the component as `*.test.tsx`
- Run `npm run build` before committing to verify no type errors
- Never import from `node_modules` paths directly — go through the package name
- Avoid default exports for utility modules; use named exports
- Use `useMemo` only when profiling shows a real bottleneck — premature memoization is banned

## Backend / infra rules

- All database migrations must be reversible and tested on a staging snapshot before production
- Use connection pooling for any Postgres client; set max connections per service
- Cache invalidation in Redis must respect the global key namespace prefix
- Background jobs go through the SQS queue, not direct cron
- Secrets are read from AWS Secrets Manager at boot; never commit `.env` files
- Deploy gates require both lint AND test passing before the workflow promotes
- Long-running migrations require a feature flag and a rollback plan in the PR

## Git + release process

- Never push directly to develop or main — always go through a pull request
- Squash-merge feature branches; rebase merge release branches
- Tag releases with `v<major>.<minor>.<patch>` after the release-notes PR lands
- Conventional-commits format is enforced by the pre-commit hook
- Run pre-commit hooks before committing; do not bypass with `--no-verify`
- Release branches freeze 48 hours before the scheduled publish

## Testing standards

- Integration tests must hit a real Postgres instance, not a mock
- Snapshot tests are banned for components; use explicit assertions
- Visual regression runs only on the main branch nightly (Percy)
- Lighthouse audits must score ≥90 on accessibility for any new public page
- Accessibility tests use axe-core via the lint job — passing it is mandatory

## Documentation

- Every public component needs an entry in the Storybook
- API changes require a CHANGELOG.md update in the same PR
- Architecture decisions go in `docs/adr/NNN-title.md` as ADR records
- Cross-team decisions get a corresponding entry in `memory.md`

## Security

- All user input passing into innerHTML must be sanitized via DOMPurify
- CSP headers required on all production routes
- Dependabot PRs auto-merge only for patch versions
- Auth tokens never appear in client-side console output, even at debug level

## Observability

- Every public route emits a `route.entry` event to Mixpanel via the analytics hook
- Errors caught by ErrorBoundary log to Sentry with the user's tenant ID stripped
- Performance marks use the `acme:*` namespace; nothing else
