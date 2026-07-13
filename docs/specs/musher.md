# snowline-musher — Autonomous Run Plugin

> **Maturity: EXPLORATORY** — registered as a governed Snowline artifact;
> graduated from draft when the first item shipped (skeleton, #11,
> 2026-07-11) per the standing maturity convention.
>
> Scope: `snowlinedev/snowline-musher` · Carrier decision: `7c317bdb`
> (supersedes `4bc92633`) · Author: Sean + session, 2026-07-09

## 1. Purpose

Musher gives Snowline the ability to *execute* work autonomously, not just
track and govern it. It dispatches headless Claude Code runs against
Snowline-governed repos and supervises their lifecycle. The name is the
posture: the musher drives the sled team and holds the reins — dispatch is
autonomous, **judgment is not**. Runs stage output for review; they never
merge.

v1 is deliberately Claude-only (decision `7c317bdb`): `claude -p` on the
operator's subscription, verified working 2026-07-09, with
`--permission-mode auto` as the enforcement envelope. The carrier seam is one
function so a second carrier (e.g. `codex exec`, cribbed from the governance
turn-runner's `_invoke_codex`) slots in wholesale later.

## 2. The Run object

The plugin's single first-class noun.

| Field | Notes |
|---|---|
| `id` | uuid |
| `objective` | the prompt/task text handed to the carrier |
| `repo` | `owner/repo` to clone |
| `base_branch` | defaults to the repo default branch |
| `scope` | Snowline slug the run is for (soft ref, optional) |
| `carrier` | enum, v1: `claude` only |
| `model` | `--model` passthrough; from the work item's `recommended_model` when dispatched off an item |
| `timeout_s` | wall clock; default generous (implementation runs are long) |
| `origin` | `mcp` \| `api` \| `watcher`, plus originating ref (work-item id / GH issue) |
| `state` | `queued → running → succeeded \| failed \| timed_out \| cancelled` |
| `workspace` | path of the run's isolated clone |
| `branch` / `pr_url` | outputs |
| `transcript_ref` | persisted stream-json transcript |
| `summary` | carrier-authored closing summary |

All terminal states are **fail-visible**: a failed or timed-out run is a
readable record with its transcript, never a silent disappearance
(turn-runner lesson).

## 3. Execution model

- **Workspace per run.** Fresh clone under
  `~/.snowline/musher/runs/<run-id>/workspace`. Never a live working copy,
  never reused across runs. Workspaces are kept after terminal states for
  autopsy and GC'd on a retention window.
- **Invocation.** `claude -p --permission-mode auto --model <model>
  --output-format stream-json` with the objective on stdin, cwd = workspace.
  The stream-json output is the transcript of record.
- **Envelope config.** The runner writes the auto-mode environment
  configuration into each workspace before invocation: protected branch =
  `base_branch`, trusted internal infra = the tailnet platform, network
  posture, sensitive locations. The default classifier rules (allow
  push-to-working-branch; soft-deny push-to-default, merge-without-review,
  self-approval, destructive/exfil classes) are relied on as the floor —
  `--dangerously-skip-permissions` is **prohibited in this codebase**.
- **Output contract.** The run prompt template instructs: implement, verify,
  commit to `musher/<run-id>-<slug>`, push, open a PR. When dispatched from a
  work item with a mirrored GitHub issue, the PR body carries `Closes #N`
  **only when the run completes the item** (partial work must not auto-close —
  standing convention). The run never merges; auto-mode enforces this even if
  the objective or a poisoned instruction says otherwise.
- **Timeout & cancellation.** SIGKILL the process group on timeout
  (turn-runner lesson); state → `timed_out`. Cancel is the same mechanism,
  state → `cancelled`.
- **Concurrency.** Sequential drain in v1 via `MUSHER_BATCH` (honest name —
  it drains a batch, it is not parallelism). Subscription usage windows are
  the real constraint; parallelism is a later, measured change.
- **Off by default.** `MUSHER_ENABLED=1` gates the whole engine (pattern:
  `SNOWLINE_SHADOW_TURNS_ENABLED`). Tests pin it off via an autouse fixture.

## 4. Surfaces

### 4.1 REST API (the spine)

Registered with the platform gateway like every plugin service; loopback-first
bind, tailnet exposure via tailscaled (platform trust decision `35546152`).

- `POST /runs` — create + enqueue. Body ≈ the Run input fields.
- `GET /runs/{id}` — full record incl. state, outputs, summary.
- `GET /runs?state=&scope=` — list/filter.
- `POST /runs/{id}/cancel`.

Everything else (MCP, watcher, other plugins like snowline-gh) is a client of
this API. No second dispatch path exists.

### 4.2 MCP surface `musher`

`musher__start_run`, `musher__get_run`, `musher__list_runs`,
`musher__cancel_run` — thin wrappers over the REST spine, composed onto the
gateway per the named-surfaces decision (`70b415fd`).

### 4.3 Work-item watcher (phase 2 — dispatch-by-API ships first)

Polls a work-item source for items **explicitly opted in at triage**
(mechanism to be settled with the source plugin — a triage destination or an
item flag; explicitly NOT "anything in the queue"). Dispatches via
`POST /runs` with the item ref as origin; maps `recommended_model` → `model`.
The watcher is deliberately dumb: judgment lives at triage time, not in the
poller.

Because the operator's PM plugin is **private** (see §5), the watcher is
written against a small **provider contract** — list opted-in items, read
their refs/model hints, mark dispatched — satisfied over the platform
gateway's surfaces, never by importing another plugin's code. The private PM
is one provider; a public deployment without it can drive musher entirely via
REST/MCP (e.g. from GitHub issues through snowline-gh).

## 5. Integrations

- **PM — with a privacy boundary.** The operator's PM plugin (snowline-pm) is
  a **private** repo; musher is public. The integration is therefore
  contract-only: gateway REST/MCP surfaces, no code-level dependency in
  either direction, and this public spec describes PM at the surface-contract
  level only. Musher must be fully usable with no PM present (REST/MCP
  dispatch stand alone). When PM *is* present: a run dispatched from a work
  item carries the item ref, and PM's existing reconcile machinery completes
  the item when the PR merges — zero new code on that side.
- **GitHub plugin (snowline-gh).** Dispatches runs against
  snowline-platform issues through the REST spine; musher does not know or
  care that the objective came from a GitHub issue beyond the origin ref.
- **Governance.** Musher records nothing into governance in v1. Runs are
  ordinary headless sessions whose reviewable output is a PR.

## 6. Security posture

- **Trust boundary.** Single-operator tailnet acceptance, same posture as the
  governance turn-runner spec §6 and platform trust decision `35546152`. The
  operator's own work items and issues are the only objective sources.
- **Enforcement layer.** `--permission-mode auto` is the envelope; the
  reviewability principle (no push-to-default, no merge, no self-approval) is
  harness-enforced rather than conventional. The runner never grants broader
  modes; there is no code path to `bypassPermissions`.
- **No containerization in v1** — the threat model that would justify it does
  not exist yet. **REVISIT triggers** (record a decision before crossing
  either): (a) any run whose objective originates outside the single-operator
  boundary (multi-user sources, external issue authors, public repos
  accepting third-party issues); (b) adding a write-capable carrier without
  an equivalent enforcement envelope (codex) — that carrier must run under
  its own sandbox flags.
- **Credentials.** Runs inherit only what the host already has (operator's
  `gh` auth, subscription login). Musher stores no secrets of its own.

## 7. Live drill — definition of done for v1

Spec-conformance is demonstrated live, not just tested (turn-runner
precedent):

1. **Happy path:** `musher__start_run` against a real repo → branch + PR
   appear; run record terminal `succeeded` with transcript + summary.
2. **Reconcile loop:** run dispatched from a mirrored work item, PR body
   `Closes #N`, merge → PM completes the item with no manual poke.
3. **Timeout:** wedge a run → process group killed → `timed_out`,
   fail-visible record, workspace preserved.
4. **Envelope:** a run whose objective says "merge your PR when done" →
   auto-mode denies the merge → run terminates `succeeded` having *reported*
   the denial instead of merging.

## 8. Phasing

1. **Skeleton** — service scaffold (uv layout per snowline-pm), platform
   registration + SDK heartbeat, DB + alembic, `.snowline`. Verification is
   local (pytest/ruff) plus the Snowline review loop — plugin repos carry no
   GitHub Actions workflows (Sean, 2026-07-11: plugins provide functionality
   for Snowline, not GitHub).
2. **Run engine** — workspace lifecycle, carrier invocation, envelope config
   injection, timeout/kill, transcript capture, `MUSHER_ENABLED` gate.
3. **Surfaces** — REST spine + `musher` MCP surface on the gateway.
4. **Review loop & drills** — PR staging prompt contract, run reporting,
   the §7 drills, README/spec maturity graduation.
5. **Watcher** — PM opt-in mechanism (settled with PM), poller,
   `recommended_model` mapping.
