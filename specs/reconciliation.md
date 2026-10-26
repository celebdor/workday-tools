# Calendar reconciliation

How wdtools keeps a Google Calendar in sync with the Workday Team Absence
Calendar without creating duplicates or ever disturbing events people added by
hand, and only for people whose Google Form opt-in decision allows it (an
explicit **Yes**, or the configured `[optin].default` fallback).

Implemented in [`wdtools/gcal.py`](../wdtools/gcal.py); the domain model and
identity live in [`wdtools/models.py`](../wdtools/models.py).

## Goals

1. **Idempotent.** Running `sync` repeatedly converges the calendar to match
   Workday — the same input always yields the same calendar state, no duplicates.
2. **Non-destructive to humans.** Events created by people are never updated or
   deleted. We only ever touch events we created.
3. **Reflects cancellations.** If an absence disappears from Workday (e.g. a
   request is withdrawn or an approval is revoked), its calendar event is removed.
4. **Privacy-preserving.** Event titles are generic (`<Person> — Out of office` /
   `<Person> — Public holiday`); the specific absence type (vacation vs sick) is
   never written out — matching what Workday's own team view shows.
5. **Opt-in gate.** An explicit `No` opts someone out and an explicit `Yes` opts
   them in. A missing response, an unmapped respondent, a response without a
   Yes/No answer, and an unconfigured form all follow `[optin].default` —
   `opt_out` (the built-in, fail-closed default) or `opt_in` (opt-out behavior).
   See [`specs/opt-in.md`](opt-in.md).

## Ownership tag

Every event wdtools creates carries private extended properties
(`_to_gcal_body`):

```json
"extendedProperties": {
  "private": {
    "source": "workday-sync",
    "wdKey": "<identity>",
    "person": "<Workday display name>",
    "personId": "<Workday worker id>"
  }
}
```

- **`source = workday-sync`** (`SOURCE_TAG`) marks the event as wdtools-managed.
  `_fetch_managed` lists events with `privateExtendedProperty=source=workday-sync`,
  so the reconcile only ever *sees* events we created. Hand-added events are
  invisible to it and therefore can never be updated or deleted.
- **`wdKey`** is the stable identity of the absence the event represents (below).
- **`person` / `personId`** are informational only, used to explain deletion
  reasons on later runs (e.g. "opted out" rather than "gone from Workday"). They
  are not part of `wdKey`. Older events without them fall back to parsing the
  summary.

Extended properties are not shown to calendar viewers, so this tagging is
invisible to the team.

## Identity: `wdKey`

`AbsenceEvent.key` (`models.py`) is a short SHA-1 of:

```
person_id | absence_type | start | end
```

- **`person_id`** is the Workday worker instance id (e.g. `247$1733`), not the
  display name — so a rename doesn't churn the calendar. Falls back to the name
  only if no id was scraped.
- **`absence_type`** is the generic label (`Out of office` / `Public holiday`).
- **`start` / `end`** are the inclusive day range of one contiguous block.

The key deliberately **excludes** the display summary and any mutable text, so a
re-run matches an existing event instead of duplicating it. Changing any field in
the key (dates, person, type) produces a *different* key and is therefore treated
as delete-old + create-new — which is correct, because it is a genuinely
different absence.

## The reconcile (`sync`)

1. Compute the window: `today - days_back` … `today + days_forward`.
2. Load opt-in decisions for this run (`optin.load_directory`). If a configured
   form cannot be read, abort before any calendar write, regardless of
   `[optin].default`.
3. `desired` = scraped absences that overlap the window **and** whose person is
   opted in, keyed by `wdKey`. Absences in the window that are not opted in are
   logged and never enter `desired`.
4. `existing` = managed events in the window (`_fetch_managed`), keyed by `wdKey`.
5. Three-way diff:

   | Condition                                   | Action                         |
   |---------------------------------------------|--------------------------------|
   | key in `desired`, not in `existing`         | **create**                     |
   | key in both, compared fields differ         | **update** in place            |
   | key in both, fields identical               | **unchanged** (no API call)    |
   | key in `existing`, not in `desired`          | **delete**                     |

   The compared fields (`_changed_fields` / `_needs_update`) are `summary`,
   `start.date`, `end.date`. Because the key already pins dates, in practice an
   update only fires when the summary text changed (e.g. a label tweak).
   Deletions are tagged with a reason: an opt-out (explicit `No`, or the
   `opt_out` fallback for missing/unmapped/no-answer) or the absence no longer
   being in Workday (`_deletion_reason`).

`SyncResult` tallies `created` / `updated` / `deleted` / `unchanged` / `opted_out`
(the latter counts in-window absences skipped because the person is opted out).

`--dry-run` performs every read and computes every decision but skips the
`insert` / `update` / `delete` calls, so it is an exact preview.

## Windowing rule

Both `desired` and `existing` are scoped to the same window, and deletes only
consider managed events **inside** it. This means:

- We never delete a managed event outside the window just because this run didn't
  scrape that far.
- Consequence: if you *shrink* `days_forward`, managed events now beyond the
  window are left in place until a future run's window covers them again. This is
  intentional (safe over tidy).

## Safety properties

- Hand-added events: never fetched, never modified. (They lack `source`.)
- First real run: `existing` is empty → all creates, zero deletes.
- Re-runs with no Workday changes: all unchanged, zero API writes.
- A cancelled Workday absence within the window → its managed event is deleted.
- Opt-out: in-window absences without an opted-in decision are never in
  `desired`, so any matching managed event is deleted. With no
  `[optin].form_id`, the `default` applies to everyone: `opt_out` (the built-in
  default) deletes all managed events in the window; `opt_in` keeps them.
- Form API failure: `optin.load_directory` raises before `_fetch_managed` or any
  insert/update/delete, so a transient failure cannot be mistaken for "nobody
  opted in", regardless of `[optin].default`.
- Latest form response wins; on a timestamp tie, `No` outranks a no-answer
  response, which outranks `Yes`.
- Unmapped form respondents are ignored; unmapped Workday people follow
  `[optin].default`.
- Target calendar is chosen by `calendar_id`; test against a scratch calendar
  before pointing at the shared team calendar.

## Observability

`sync` logs the whole reconcile:

- **INFO** (`-v`): window + source tag, counts of desired and managed events, one
  line per opt-in response (`email = Yes/No (Workday person)`), the opt-in summary
  (responses, explicit Yes/No, no response, no Yes/No answer, unmapped, default),
  one line per opted-out absence, and one line per `create` / `update` / `delete`
  (prefixed `[dry-run]` when previewing; deletes include the reason), then the
  final `SyncResult`.
- **DEBUG** (`-vv`): the `_fetch_managed` query, the resolved opt-in
  response/mapping box, the `wdKey` behind each decision, the exact
  `field: old -> new` diff on updates, and every `unchanged` event.
