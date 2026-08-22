# Contributing

## Workflow

1. Branch off `main`, open a PR.
2. CI (`.github/workflows/ci.yml`) must pass: backend tests (pytest) and a
   frontend JS syntax check.
3. At least one approving review is required before merging (see branch
   protection settings on `main`).
4. Merge with squash so `main` has one commit per PR.

## Commit / PR title format

Releases and changelogs are generated automatically by
[release-please](https://github.com/googleapis/release-please) from your
PR's squash-commit message, so it must follow
[Conventional Commits](https://www.conventionalcommits.org/):

- `feat: ...` — new feature (bumps minor version)
- `fix: ...` — bug fix (bumps patch version)
- `feat!: ...` or a `BREAKING CHANGE:` footer — breaking change (bumps major version)
- `chore:`, `docs:`, `refactor:`, `test:`, `ci:` — no version bump, still shows in changelog under its own section

## Releases

release-please watches `main` and keeps an up-to-date "Release PR" open
that accumulates changes since the last release. Merging that PR:

- tags the release (`vX.Y.Z`)
- publishes a GitHub Release with an auto-generated changelog
- updates `CHANGELOG.md`

No manual version bumping or tagging needed — just merge the release PR
when you're ready to ship.
