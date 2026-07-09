# snowline-musher

**Autonomous run plugin for [Snowline](https://github.com/snowlinedev/Snowline).**

The musher drives the team; judgment stays with the human. Musher dispatches
and supervises headless [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
runs against work items in your tracker: each run gets a fresh, isolated
workspace, executes under Claude Code's `--permission-mode auto` enforcement
layer, and stages its output as a branch and pull request — **never a merge**.
Review and merge stay with you.

## What a run is

A run = { objective, target repo, base branch, model, timeout } → executes
headless → { branch, PR, transcript, status, summary }.

One engine, three dispatch surfaces:

1. **REST API** — `POST /runs`, the integration spine other Snowline plugins
   call (e.g. dispatching a run against a GitHub issue or a PM work item).
2. **MCP tools** — `musher__start_run` / `get_run` / `list_runs` /
   `cancel_run`, so any agent session can dispatch and supervise runs.
3. **Work-item watcher** (phase 2) — picks up tracker items explicitly opted
   in at triage and dispatches them through the same API.

## Safety posture

- No `--dangerously-skip-permissions`, anywhere. Runs execute under
  Claude Code's `auto` permission mode: push-to-working-branch allowed;
  push-to-default-branch, merge-without-review, self-approval, and
  destructive/exfiltration action classes denied at the harness level.
- Every run works in a fresh clone in an isolated workspace directory —
  never a live working copy.
- Autonomous output is staged for review (branch + PR). The reviewability
  principle is enforced by the permission layer, not just convention.

## Status

Spec-first, under active development. See
[`docs/specs/musher.md`](docs/specs/musher.md) for the governing spec.

## License

Apache-2.0
