# Musher work-item provider contract — v1

> **Maturity: EXPLORATORY** — the contract the musher watcher (spec §4.3)
> polls; settled with the operator's PM plugin 2026-07-18 but written so ANY
> work-item source can implement it.
>
> Scope: `snowlinedev/snowline-musher` · Parent spec: `musher.md` §4.3/§5
> (artifact `ae8987e3`) · Settled by decision `1a8bdf16` · Trust posture:
> platform decision `35546152`

## 1. Why a contract

The musher watcher dispatches runs from work items, but the operator's PM
plugin is **private** while musher is public (spec §5). The watcher is
therefore written against this small HTTP contract — served over the platform
gateway by whatever wants to feed musher — and never against another plugin's
code. The private PM is one provider; a public deployment can implement this
contract from any item source (e.g. GitHub issues via snowline-gh), or run no
provider at all and drive musher purely through REST/MCP.

One provider per musher deployment in v1: the watcher polls a single base URL.

## 2. Opt-in model (settled)

Dispatch eligibility is a **per-item flag on the provider side, set explicitly
at or after triage** — NOT a triage destination, and never "anything in the
queue" (spec §4.3):

- **Explicit.** A human (or a policy the provider owns) marks each item
  dispatchable. The judgment about *whether* an item is safe/suited to an
  autonomous run lives at triage time, with the person; the watcher stays
  deliberately dumb.
- **A flag, not a destination.** Opt-in is orthogonal to the provider's own
  prioritization: flagging an item for dispatch must not move it in, out of,
  or around the provider's roadmap, and un-flagging must be equally free.
  Contract-side this is invisible — the provider simply serves its current
  dispatch queue; how the flag is stored/set is the provider's business.
- **Dispatch-ready.** An opted-in item may still be undispatchable (no target
  repo known). The provider must exclude such items from the queue rather than
  serve records musher cannot act on.

## 3. Endpoints

All paths are relative to the provider's configured base URL (§4). JSON in
and out. No auth fields in v1 — the single-operator tailnet is the boundary
(musher spec §6, platform trust decision `35546152`).

### 3.1 `GET /provider/work-items`

The dispatch queue: every item that is opted in (§2), dispatch-ready, and not
yet marked dispatched (§3.3). No query parameters in v1 — the provider, not
the watcher, decides membership.

```json
{
  "items": [
    {
      "id": "6f1f2f66-2f6e-4d4e-9a3a-000000000042",
      "title": "Fix the retention-window off-by-one",
      "objective": "GC deletes workspaces a day early when …",
      "repo": "acme/widget",
      "issue_number": 42,
      "model": "sonnet",
      "scope": "acme/widget"
    }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string, required | Opaque, stable, unique within the provider. Musher stores it verbatim as `Run.origin_ref` — it is the dedupe key (§5). |
| `title` | string, required | One-line item title. |
| `objective` | string, required | The task text the run prompt is built from. The provider sends what it wants executed; musher does not enrich it. |
| `repo` | string, required | `owner/repo` the run clones. An item without one is not dispatch-ready (§2). |
| `issue_number` | integer or null | The item's mirrored GitHub issue in `repo`, when one exists — the run's PR uses it for `Closes #N` (musher spec §3). |
| `model` | string or null | Raw model hint (e.g. a `recommended_model`). Mapping hint → `--model` is musher's business, not the contract's. |
| `scope` | string or null | Snowline scope slug for the run record (soft ref, musher spec §2). |

Unknown extra fields must be ignored by the watcher (providers may carry
their own annotations).

### 3.2 `GET /provider/work-items/{id}`

One record, same shape as a §3.1 entry plus dispatch state — served even
after dispatch (it has left the queue but remains readable):

```json
{ "...": "...", "dispatched": true, "run_id": "0d0c…" }
```

`404` for any other id — deterministically, an id is servable iff it is
currently in the queue (§3.1) or marked dispatched (§3.3). Items that exist
provider-side but were never opted in (or were un-flagged before dispatch)
are indistinguishable from nonexistent ones: a provider with a private
backlog must not let this surface enumerate it.

### 3.3 `POST /provider/work-items/{id}/dispatched`

Body `{"run_id": "<musher run id>"}`. Marks the item dispatched, removing it
from §3.1. Idempotent, first-wins:

- first call → `200`, mark recorded;
- repeat with the **same** `run_id` → `200`, no-op;
- a **different** `run_id` → `409` — the earlier dispatch stands, and the
  caller must not treat the item as its own;
- an id not servable under §3.2's rule → `404`.

Marking dispatched is a statement that a run exists, not that it succeeded.
**Re-arming** a dispatched item (after a failed/abandoned run) is a
deliberate provider-side act — clearing its mark puts it back in the queue;
the watcher never re-dispatches on its own judgment.

## 4. Watcher configuration

`MUSHER_PROVIDER_URL` names the provider's base URL. Unset (the default) the
watcher stays dark — a deployment with no provider loses nothing else;
REST/MCP dispatch stands alone (musher spec §5). The whole engine remains
behind `MUSHER_ENABLED` regardless.

## 5. Dispatch sequence & failure posture

For each item in the queue the watcher: (1) skips it if a musher run already
exists with `origin_ref == id` — the belt-and-braces dedupe when a previous
mark call failed; (2) creates the run via its own `POST /runs` with
`origin=watcher`, `origin_ref=id`, and the item's `repo`/`objective`/`model`/
`scope`; (3) marks it dispatched (§3.3). A provider that is down or erroring
just means no dispatch this poll — the watcher never queues intent, and every
call is safe to repeat.

## 6. Versioning

This is contract v1. Changes are settled the same way this version was: a
governance decision on `snowlinedev/snowline-musher` plus a revision of this
document (and its registered artifact) — never silently.
