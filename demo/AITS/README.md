# AITS — Acme Internal Tooling Suite

The component library, design tokens, and shared frontend conventions used by
every internal Acme product (the admin console, the operator dashboard, the
support tooling, the marketing CMS). Owned jointly by Design Systems and
Platform Frontend.

## Install

```bash
npm install @acme/aits
```

## Usage

```tsx
import { Button, Modal } from "@acme/aits";
import "@acme/aits/tokens.css";

export function Example() {
  return <Button variant="primary">Save changes</Button>;
}
```

## What's in here

- `src/components/` — production React components (Button, Modal, …). Each
  component co-locates its `.tsx`, `.css`, and `.test.tsx` files.
- `src/styles/tokens.css` — design tokens (color, spacing, radius, type).
  The source of truth for the visual system; never hardcode pixel values
  in component CSS.
- `.claude/CLAUDE.md` — repo conventions Claude must follow when working
  in this codebase. Also pulls in `rules.md` and `memory.md` at the root.
- `rules.md` — extended contributor conventions (test policy, review flow,
  release cadence, internationalization).
- `memory.md` — recent product decisions, accepted ADRs, known issues.
  Updated by the team after every quarterly review.

## Contributing

1. Branch off `develop`.
2. Add or modify a component; co-locate the test file.
3. Run `npm run lint && npm test && npm run build` locally.
4. Open a PR. The release manager will squash-merge once approved.

See `.claude/CLAUDE.md`, `rules.md`, and `memory.md` for the full context
Claude operates within.

## Releases

- Versions follow `v<major>.<minor>.<patch>` semver.
- Release notes land in `CHANGELOG.md` in the same PR as the version bump.
- Tagged releases publish to the internal npm registry via the
  `release.yml` workflow.

## License

Internal — do not redistribute outside Acme.
