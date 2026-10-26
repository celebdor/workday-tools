# Calendar-sync opt-in

Calendar sync is privacy-sensitive: a shared team calendar can reveal that
someone is away even when the underlying absence type is hidden. `wdtools` can
therefore gate calendar sync on a Google Form and apply each person's latest
answer.

An explicit **Yes** always opts someone in and an explicit **No** always opts
them out. Everyone else — no submission, no Yes/No answer, or no matching
`[optin.people]` entry — follows `[optin].default`:

- `opt_out` (the built-in default): only a decisive **Yes** syncs. This keeps a
  misconfigured form or missing response fail-closed.
- `opt_in`: people stay synced until they explicitly answer **No** (opt-out
  behavior). Use this when the calendar should include everyone by default.

Implemented in [`wdtools/optin.py`](../wdtools/optin.py) and enforced at the top of
[`wdtools/gcal.py:sync`](../wdtools/gcal.py). The reconcile mechanics are described
in [`specs/reconciliation.md`](reconciliation.md).

## Policy

| State | Result |
|-------|--------|
| Current response is **Yes** | Opted in — absences sync normally |
| Current response is **No** | Opted out — no sync; existing in-window managed events deleted |
| No form submission | Follows `[optin].default` |
| Response without a Yes/No answer | Follows `[optin].default` |
| Respondent email not in `[optin.people]` | Ignored; the Workday person follows `default` |
| Workday person not in `[optin.people]` | Follows `[optin].default` |
| `[optin].form_id` empty | `default` applies to **everyone** |

With `default = "opt_out"` (the default), all the "follows default" rows mean
opted out and in-window managed events are deleted. With
`default = "opt_in"`, they mean opted in and events are kept unless an explicit
**No** applies.

The latest submission wins. On a timestamp tie, `No` outranks a no-answer
response, which outranks `Yes`. A response containing both Yes and No is treated
as **No** and logged as a warning.

The built-in default is intentionally fail-closed: it is better to remove events
for someone who wanted to be synced (they can resubmit and the next run restores
them) than to leak an opted-out person's absences. `default = "opt_in"` is the
explicit opt-out mode, so choose it deliberately.

## Form requirements

Create one Google Form with:

1. **One single-select question** (dropdown, multiple choice, or radio; the
   Forms API reports all of them as `textAnswers`), e.g. *"Do you want your PTO
   and public holidays synced to the shared calendar?"*, with exactly two options:
   `No` and `Yes`. The parser compares the answer text case-insensitively after
   stripping whitespace; `Yes.` is not `Yes`.
2. **Collect email addresses** enabled (Settings → Responses). The Forms API
   returns `respondentEmail`; without it the response cannot be matched.
3. Ideally, restrict responses to the organization so respondent emails are
   trustworthy.

Get the form ID from the edit URL:
`https://docs.google.com/forms/d/<FORM_ID>/edit`.

The tool reads only responses (`forms.responses.readonly`); it does not need to
read the form body or question metadata. If a response contains both `Yes` and
`No` (e.g. because the form was changed later), it is treated as **No** and a
warning is logged.

## Configuration

```toml
[optin]
form_id = "<FORM_ID>"
# "opt_out" (default) or "opt_in"; controls the no-decisive-answer fallback.
default = "opt_out"

[optin.people]
"person@example.com" = "Person Name"       # exact Workday display name
# or the Workday worker instance id:
# "other@example.com" = "247$1733"
```

- `[optin]` is optional in the TOML sense. Leaving `form_id` empty applies
  `default` to everyone. With the built-in `opt_out`, that is a breaking change
  for upgrades from versions without the gate; preview with `sync --dry-run -v`.
- `default` accepts `opt_out` / `opt-out` / `opted_out` and
  `opt_in` / `opt-in` / `opted_in` (case-insensitive). Other values are
  rejected by `Config.load` / `validate_optin`.
- `[optin.people]` must be non-empty when `form_id` is set; `Config.validate_optin`
  rejects the combination otherwise. Without a mapping, responses cannot be
  attributed to Workday people, so the form cannot apply an explicit **No**.
- Keys are respondent emails (case/whitespace-insensitive). Values are either the
  exact Workday display name shown on absence events or the worker instance id
  (`247$…`). Name matching is case- and whitespace-insensitive.
- The map is manual because Workday's absence payload exposes only display names
  and worker ids, while the form exposes respondent emails. There is no
  automatic Workday email lookup.
- With `default = "opt_in"`, people who are not in the map also stay synced.
  Adding mappings is still required to honour explicit **No** responses.

## Reading responses

`optin.load_directory(cfg)`:

1. Calls `validate_optin()`. If `form_id` is empty, returns an
   `OptInDirectory` with `configured=False`, no responses, and the configured
   `default`. Every `is_opted_in` call returns the default decision.
2. Otherwise authenticates with the `forms.responses.readonly` scope, using a
   separate token at `google_forms_token_path` (`forms_token.json` in the state
   dir). `wdtools setup-google` runs this consent ahead of time when a form is
   configured.
3. Paginates `forms.responses.list` (`pageSize=1000`) and collects
   `respondentEmail`, `lastSubmittedTime`, and every `textAnswers` value.
4. Maps each email through `[optin.people]`. Missing emails and unknown emails
   produce unmapped responses.
5. Resolves one latest response per mapped Workday target. If multiple emails map
   to the same target, the latest submission across them wins.
6. Logs the responses and decisions (see below).

## Decision rules

`OptInDirectory` keeps:

- `responses` — every parsed response, with email, mapped target, answer
  (`True`/`False`/`None`), raw answer text, and submission time.
- `decisions` — the latest response per normalized target. Newer
  `lastSubmittedTime` wins. On a timestamp tie, `No` outranks a no-answer
  response, which outranks `Yes`.
- `_targets` — every normalized value in `[optin.people]`, used to distinguish
  "not mapped" from "mapped but no response".

For an `AbsenceEvent`, matching checks both `event.person` and, when present,
`event.person_id` against the configured targets. `is_opted_in(event)`:

- returns `False` when the latest matching response is **No**;
- returns `True` when it is **Yes**;
- returns `default == "opt_in"` when there is no matching decision or the latest
  response has no Yes/No answer.

`explain(person, person_id)` returns the human-readable reason used in logs:

- no form configured, opted in by default / everyone opted out;
- not mapped in `[optin.people]`, possibly opted in by default;
- no form response, opted in by default / opted out;
- opted out (No from `<email>`);
- opted out (response from `<email>` has no Yes/No answer);
- no Yes/No answer from `<email>` (opted in by default);
- opted in (Yes from `<email>`).

The explicit-response properties (`opted_in_people`, `opted_out_people`) only
count decisive answers. `no_answer_people`, `no_response_people`, and
`defaulted_people` make the fallback visible in logs and the debug box.

## Reconcile integration

`gcal.sync` loads the directory **before** creating the calendar service or
fetching managed events:

```python
directory = optin.load_directory(cfg)
service = _service(cfg)
...
desired = {ev.key: ev for ev in in_window if directory.is_opted_in(ev)}
```

In-window absences that are not opted in are logged and collected in
`opted_out_reasons` (keyed by `wdKey`). They are never in `desired`, so the
normal reconcile deletes any matching managed event. `SyncResult.opted_out`
counts the skipped absences.

Deletion reasons (`_deletion_reason`) are:

- `opted out (no opt-in form configured)` when `form_id` is empty and the
  default is `opt_out`;
- `no longer in Workday` when `form_id` is empty and the default is `opt_in`
  (no form can opt anyone out);
- `directory.explain(person, person_id)` when the key matches an in-window
  opted-out absence, or when the event's `person`/`personId` extended properties
  (or a fallback parse of its summary) identify an opted-out person;
- `no longer in Workday` otherwise.

The reason is appended to the INFO delete log, e.g.:

```
delete: Person Name — Out of office [opted out (No from person@example.com)]
```

## Logging

- **INFO**: one line per response, e.g.
  `Opt-in response: person@example.com = Yes (Person Name)`; a summary with
  response / explicit Yes / explicit No / no response / no Yes/No answer /
  unmapped counts and the active default; one line per opted-out absence; every
  deletion with its reason.
- **DEBUG** (`-vv`): a bordered box styled like the resolved-configuration box,
  containing the form ID, active default, counts, the email→name mapping, every
  response, and every resolved decision.

The response and mapping data contain employee emails and names; treat the logs
with the same care as the config directory.

## Failure and safety

- **Configured form unreadable** (Forms API disabled, wrong ID, missing consent,
  network error): `load_directory` raises `RuntimeError` and `sync` aborts before
  any calendar read/write, regardless of `default`. A transient failure is never
  mistaken for "nobody opted in".
- **No form configured**: `default` applies. `opt_out` is fail-closed; `opt_in`
  preserves the older "sync everyone" behavior.
- **Form configured but zero responses**: valid; `default` applies to everyone.
- **Dry run**: still reads the form (read-only) and logs decisions, but makes no
  calendar writes.
- **Unmapped responses**: ignored with a warning; the Workday person follows
  `default`.
- **Missing respondent email**: response is unmapped and ignored.
- **Unmapped Workday person**: follows `default`, because no form response can be
  attributed to them. With `opt_in` they remain synced.

## Limitations

- The mapping is manual and must be maintained when people join, leave, or
  change their Workday display name.
- With `default = "opt_in"`, unmapped people and non-responders are synced. An
  explicit **No** can only remove someone whose email is mapped.
- Trust depends on the form collecting verified respondent emails; a respondent
  who can enter someone else's email could opt them in/out. Restrict the form to
  the organization.
- The gate covers all absence events for that person, including public holidays,
  because the Workday calendar represents them per person.
- Responses are read on every `sync`; there is no local cache. This keeps the
  gate current but requires the Forms API/token to stay available.
