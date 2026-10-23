# Quarterly Connections → Google Doc

How wdtools reads Quarterly Connection reviews from Workday "My Tasks" and writes
each associate's answers into a shared Google Doc for the manager to respond in.

Read side in [`wdtools/qc.py`](../wdtools/qc.py); write side in
[`wdtools/gdocs.py`](../wdtools/gdocs.py); domain model in
[`wdtools/models.py`](../wdtools/models.py). This is a separate utility from the
absence sync and shares only the Workday session and the Google OAuth client.

## Goals

1. **Faithful extraction.** For every `Quarterly Connection: <Employee>` task,
   capture each evaluation section's questions and the associate's answers.
2. **Correct placement.** Each connection lands in the Google Doc tab named for its
   review period, `YEARQX QC` (Q1 Jan–Mar, Q2 Apr–Jun, Q3 Jul–Sep, Q4 Oct–Dec;
   year = year of the review period).
3. **Idempotent & non-destructive.** Re-running never duplicates an employee and
   never overwrites a manager's typed response.

## Read side (Workday)

The My Tasks inbox lists a task per review titled `Quarterly Connection: <Employee>`.
`scrape_quarterly_connections`:

1. opens `talent.my_tasks_url` and **parses the captured `queryTasks` JSON** (not the
   DOM): each Quarterly Connection entry carries
   `appliedSystemTags.TITLE.value` ("Quarterly Connection: &lt;Employee&gt;"),
   `appliedSystemTags.EFFECTIVE_DATE.value` (period end `YYYY-MM-DD` → `(year,
   quarter)` via `period_to_quarter`), and
   `unifiedTaskModel.taskExecutor.taskToExecute.uri` (the overview URI);
2. per task: navigates the overview URI and clicks **Get Started** (falling back to
   navigating the Get Started `commandButton` URI parsed from the overview JSON);
3. in the task **wizard**, reads the left-rail sections
   (`data-automation-id='verticalNavigationItem'`), skips `Review and Submit`
   (`SKIP_SECTIONS`), and for each section pairs the read-only rich-text blocks
   (`data-automation-id='richTextContent'`) two-by-two as (question, associate
   answer). The manager's own answer box is an empty `richTextContent` and is
   dropped.

**Tenant-specific selectors.** The above was wired against a real Red Hat tenant
capture. On another tenant, re-derive with `wdtools qc-snapshot --task 1`, which
dumps the inbox, the task overview, the editor, each embedded frame, and the
embedded form as HTML + screenshots + captured XHR/JSON/document responses (shared
capture helper in [`wdtools/_capture.py`](../wdtools/_capture.py)), then adjust
`SELECTORS` in `qc.py`.

## Write side (Google Docs)

### Tabs cannot be created via the API

The Google Docs API can read tabs and write into existing ones, but has **no**
request to create a tab (an open, unimplemented feature request on Google's
tracker). So the per-quarter tabs (`2026Q1 QC`, …) are created by hand once per
quarter. `gdocs.find_tab` locates the tab by title (recursing child tabs); a
missing tab is a hard error that lists the tabs that do exist.

### Layout written per employee

Within the quarter tab, appended at the end of the tab body:

- `Heading 1` — employee name
- the associate's content: per section `Heading 2` — section name; per question
  `Heading 3` — question text, then the associate's answer as **formatted body**.
- a dedicated `Heading 2` — **Manager Response** section, with a `Heading 3` per
  associate section and an empty paragraph beneath each for the manager to type into.
  This section is kept separate so `--replace` can preserve it (see below).

**Rich-text answers.** The associate's answer is captured as HTML and translated by
[`wdtools/richtext.py`](../wdtools/richtext.py) (`html_to_paras`) into paragraphs and
runs, preserving bold, italic, links, and bullet lists, and dropping Workday's empty
spacer paragraphs. Answer-internal sub-headings (`<h2>`/`<h3>`) become **bold
paragraphs**, not Docs headings, so they don't pollute the employee/section/question
outline. `_build_requests` emits one `insertText` (newline-terminated paragraphs)
plus a paragraph-style request per paragraph, an `updateTextStyle` per
bold/italic/linked run, and a `createParagraphBullets` per run of consecutive bullet
paragraphs — all targeted at the tab via `tabId`, with ranges computed from the known
insert index.

## Idempotency & safety

Before appending an employee, `_employee_already_present` scans the tab for a
`HEADING_1` paragraph with that name; if found, the employee is **skipped** — never
re-inserted or edited — so manager responses are never clobbered and re-runs never
duplicate. On a real run the doc is re-fetched between employees so a tab that grew
earlier in the same run has fresh indices.

`qc-sync --replace` opts out of the skip and rewrites an existing employee. If the
employee's **Manager Response** section exists (`_manager_section_start`), it deletes
only the associate content before it (`HEADING_1` → that H2) and rewrites that part,
**preserving the manager's responses**. If there's no such section (a brand-new
employee, or an old-format block), it rewrites the whole block
(`HEADING_1` → next `HEADING_1`, via `_employee_range`), including a fresh Manager
Response section. Re-importing to refresh the associate's answers is therefore safe
even after managers have responded.

## Caching

An associate's answers don't change once submitted, so scraped connections are cached
in the state dir (`qc_cache.json`, keyed by each task's overview URI). `qc-scrape` /
`qc-sync` still load the inbox to discover tasks, but reuse cached answers and only
drive the wizard for tasks not yet cached. `--refresh` re-scrapes every task and
rewrites the cache; `--offline` skips Workday entirely (no session, no inbox) and
works purely from the cache — handy for iterating on the Doc output. See
`_load_cache` / `_save_cache` / `load_offline_connections` in `qc.py`.

## OAuth scope

The Docs side uses its own scope (`.../auth/documents`) and token file
(`docs_token.json`), separate from the calendar sync's `calendar.events` token, so
the two features never disturb each other's authorization.
