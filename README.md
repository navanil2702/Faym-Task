# Faym — Automated Multi-Item Returns Agent

A browser agent that reads pending return tasks from Excel, places the returns on
Amazon or Flipkart, and writes the outcome back **per line item**.

Built on Python + Playwright driving the **real installed Google Chrome** with a
persistent profile, because a genuine browser and a genuine long-lived session
are the two things that most keep this from reading as a bot.

---

## Quick start

```bash
pip install -r requirements.txt && python3 -m playwright install chromium
```

Look at what the agent parsed out of the sheet, without touching a browser:

```bash
PYTHONPATH=src python3 -m faym_returns.cli "~/Downloads/Faym Status Test Orders.xlsx" --inspect
```

Plan a run offline (no browser, writes a results workbook):

```bash
PYTHONPATH=src python3 -m faym_returns.cli "~/Downloads/Faym Status Test Orders.xlsx" --offline
```

Dry run in a real browser — walks every return flow but never clicks the final
confirm. **This is the default**; there is no way to submit a return by accident:

```bash
PYTHONPATH=src python3 -m faym_returns.cli "~/Downloads/Faym Status Test Orders.xlsx" --limit 1
```

Place returns for real. Requires the explicit flag *and* a typed confirmation:

```bash
PYTHONPATH=src python3 -m faym_returns.cli "~/Downloads/Faym Status Test Orders.xlsx" --live --limit 1
```

Run the tests:

```bash
python3 -m pytest -q
```

---

## Control panel (web UI)

For operators who shouldn't have to touch a CLI:

```bash
python3 serve.py
```

Then open **http://127.0.0.1:8000**. From there you can point at a workbook (or
upload one), review the parsed line items *before* anything runs, start a dry run
or a live run, watch progress stream in as it happens, and download the results.

It drives the same `Orchestrator` the CLI does — same browser behaviour, same
safety rails, same Excel write-back — so nothing behaves differently depending on
which front end you use.

What the panel adds over the CLI:

| | |
|---|---|
| **Pre-flight review** | Every order expands to its line items with the SKU, window, eligibility and any parse warnings, so a bad row is caught before a browser opens. |
| **Live progress** | Per-item state (queued → working → outcome), the countdown during the deliberate pauses, and an activity feed, over server-sent events. |
| **OTP handoff** | When the agent needs a login it says so in the UI and waits; you type the phone number and OTP in the Chrome window it opened. |
| **Stop button** | Aborts cooperatively at the next pause — never mid-submission, so it can't leave a half-filed return behind. |
| **Failure screenshots** | Thumbnails inline, click to open full size. |
| **Plan only** | Parses and classifies with no browser at all. The quickest way to sanity-check a freshly-pasted sheet. |

Two behaviours worth knowing:

- **It binds to `127.0.0.1` on purpose.** The panel can drive a browser holding a
  live logged-in shopping session, so it must not be reachable from the network.
  There is no authentication because there is no remote access.
- **One run at a time.** A second start request gets a `409`. Two concurrent
  browser sessions against one account is a reliable way to get that account
  flagged, so the server refuses rather than queueing.

A live run needs the phrase `place returns` typed into the confirm dialog, exactly
as the CLI does. Without it the API returns `400` and nothing is submitted.

**"Start fresh"** controls whether you resume. Left off, the panel and the run
both read the existing results workbook, so rows already marked Done are not
re-attempted — and the counts you see are the counts that will run. Tick it to
re-run from the original sheet.

---

## Read this before the first live run

**1. The selectors need one supervised pass against the live sites.**
Every selector lives in [`src/faym_returns/selectors/`](src/faym_returns/selectors/)
as an *ordered candidate list* — accessibility role and visible text first, CSS
last. They were authored from the documented flow but have **not** been confirmed
against the live DOM, because doing so requires signing into the real account.
Run `--limit 1` in dry-run mode, watch the browser, and fix whatever misses in the
YAML. That is a config edit, not a code change.

**2. Every order in the test dataset is already out of window.**
The delivery dates are late June / early July 2026 with 7–10 day windows; today is
past all of them. The agent correctly refuses all 14 items. Use
`--today 2026-07-06` to exercise the pipeline as it would have behaved when the
orders were live.

**3. Login is yours, not the agent's.**
The agent never handles a password or an OTP. It opens the login page, prints
instructions, and waits while you sign in by hand in the visible Chrome window.
Because the profile persists, this happens once — not every run. Real human
keystrokes on the login form are also the least detectable way to authenticate.

---

## How the data is interpreted

The source sheet is **order-level** — one row per order, with every product URL
crammed into a single `Product Link` cell — but the spec requires an outcome per
SKU. So the agent explodes each order row into line items. On the test dataset,
**7 order rows become 16 line items, 14 of them actually ordered.**

Two conventions in that cell carry real meaning:

| Convention | Meaning | Why it matters |
|---|---|---|
| A bare `NA` after a URL | That item was **not** ordered | Rows 6 and 8 each hold five links but declare `No of Product = 4`; in both, the extra link is the one trailed by `NA`. Reading it as ordered would file a return for something never bought. |
| The `pid=` query param | The line-item identity | The sheet has no SKU column. The pid is what distinguishes four near-identical shoulder bags on one order. |

The parser also survives what is actually in those cells: WhatsApp export prefixes
(`[8:23 pm, 26/06/2026] Arti Faym C: Take a look at this…`), a Meesho promo blurb,
a stray `size xxl` note, dirty windows (`10 Days`, `7 Days `, `10 Day`), and
delivery dates typed as ranges (`5-6 July`). Extracted counts are cross-checked
against `No of Product` and a mismatch is flagged for a human rather than guessed.

The order-level `Amount` is **never** divided to estimate a per-item refund.
Refund amounts are only ever recorded as the platform reports them.

---

## Write-back

Results land in a **copy** of your workbook under `data/`. The original file in
`~/Downloads` is never modified.

- **`Line Items`** (new sheet) — one row per SKU: Return ID, Return Status,
  Refund Amount, Task Status, Timestamp, Log, and a `Source Row` back-pointer.
  This is the record of truth.
- **`Sheet1`** (your original) — each order row gets a rolled-up summary in the
  existing `Refund ID` / `Return Status` / `Refund Amount` / `Timestamp` / `Log`
  columns, so the sheet stays readable at a glance.

Re-running updates rows in place, keyed on `(Order ID, SKU)`. It never appends
duplicates.

### Statuses

| Return Status | Meaning | Task Status |
|---|---|---|
| `Placed` | Return filed; ID captured | Done |
| `Already Cancelled & Refunded` | Platform had already refunded it | Done |
| `Out of window` | Past its return window | Done |
| `Not yet delivered` | Cannot return yet; re-run later | Done |
| `Not ordered (NA)` | Marked `NA` in the sheet | Done |
| `Support Needed` | No return control; needs chat support | **Needs human review** |
| `Item not found on order` | SKU not matched on the order | **Needs human review** |
| `Failed` | Error mid-flow; nothing submitted | **Needs human review** |
| `Planned (not attempted)` | Offline plan only | Pending |

`Failed` and `Planned` are the only non-final states, so only those requeue on the
next run. Anything unrecognised in the column — including statuses an operator
typed by hand — is treated as settled, because filing a duplicate return is the
more expensive mistake.

---

## Partial success

Line items are processed independently, which is the whole point:

- An item past its window is logged and skipped; the rest of the order proceeds.
- An order row is marked `Done` only once **every** line item holds a final state.
- If any item needs a human, the order row is flagged for review — never silently
  dropped.
- Items marked `NA` are excluded from the roll-up entirely, so a never-ordered
  link cannot drag an otherwise-complete order into review.

On the test dataset, order `OD337974610559997100` demonstrates this: five links,
one `NA`, four returns attempted.

---

## Batch vs sequential flows

The agent detects which model a platform uses and follows the right path.
`process_order()` returns an outcome **per SKU** either way, so write-back stays
line-item oriented regardless.

- **Flipkart — sequential.** No multi-item wizard exists; each SKU has its own
  Return control and its own window, so the micro-flow repeats per item.
- **Amazon — batch, with sequential fallback.** The "Return or replace items"
  wizard lists every item with a checkbox. When two or more are present the
  adapter selects them and makes one pass; otherwise it falls back to per-item.
  A batch return ID is shared across the items it covers, and the log says so.

---

## Not getting flagged as a bot

Detection keys on inhuman *timing*, *pointer paths* and *throughput* far more than
on fingerprint trivia, so that is where the effort goes.

**Session and fingerprint**
- Real installed Chrome (`channel="chrome"`), not bundled Chromium.
- A persistent `user_data_dir`, so cookies and device trust survive between runs.
  Repeatedly logging in from a clean fingerprint is itself a strong bot signal.
- Headless is **refused outright** — it is trivially detectable.
- `en-IN` locale, `Asia/Kolkata` timezone and Gurgaon geolocation, so the
  fingerprint agrees with the account's real-world context.
- Automation flags suppressed via `--disable-blink-features=AutomationControlled`
  and `ignore_default_args=["--enable-automation"]`.

**Deliberately minimal patching.** Verified on a real navigation, this setup
already reports `navigator.webdriver === false` (the genuine Chrome value), a real
`PluginArray` with the actual PDF Viewer entries, real WebGL vendor passthrough
and a real core count. So the init script patches almost nothing. Forcing
`navigator.webdriver` to `undefined` — as most stealth snippets do — is *itself* a
tell, because no real browser reports undefined; and faking `navigator.languages`
desynchronises it from the `Accept-Language` header Playwright derives from the
locale. The only correction applied is normalising `webdriver` to `false` on the
bundled-Chromium fallback path.

**Behaviour**
- Pointer moves along an eased Bézier arc in many small steps, decelerating into
  the target, and clicks land off-centre with a realistic press duration.
- Typing is per-character with variable delay and occasional longer pauses.
- Scrolling happens in uneven bursts, not one jump.
- Reading pauses before acting; aimless movement between items.

**Throughput**
- 12–38 s between line items, 25–70 s between orders, and a 2–5 minute break
  every 6 items.
- Hard cap of 25 line items per session; the rest stay Pending for a later run.
- Quiet hours (01:00–07:00 by default) block a run outright — nobody files
  returns at 04:00.
- Never parallelised against one account.

**On challenge**
A captcha or block page **aborts the session** rather than retrying. Hammering a
challenge is what turns a soft flag into a hard ban. Unfinished items stay
Pending, and a screenshot is saved.

Pacing is seeded (`--seed`) so a failure can be reproduced. `--fast` compresses
the delays for debugging and should never point at a live site.

---

## Useful flags

| Flag | Effect |
|---|---|
| `--live` | Actually submit. Off by default; also needs a typed confirmation. |
| `--offline` | Plan only; no browser. |
| `--inspect` | Print parsed line items and exit. Writes nothing. |
| `--limit N` | Cap line items this run. Use `--limit 1` for the first live test. |
| `--order ID` | Restrict to one order (repeatable). |
| `--platform` | Restrict to `Amazon` or `Flipkart` (repeatable). |
| `--today DATE` | Override today's date for the window check. |
| `--no-resume` | Re-attempt items that already hold a final status. |
| `--seed N` | Seed the timing RNG for reproducible runs. |
| `--allow-quiet-hours` | Permit a run inside quiet hours. |
| `--grace-days N` | Slack before declaring an item out of window (default 1). |

---

## Layout

```
serve.py            start the control panel (no PYTHONPATH needed)
src/faym_returns/
  cli.py            command-line entry point; dry-run by default
  orchestrator.py   run loop, planning, partial-success handling
  normalize.py      messy-cell parsing, order row -> line items
  eligibility.py    return-window pre-filter
  workbook.py       Excel read + per-line-item write-back
  browser.py        persistent Chrome session, fingerprint, challenge detection
  humanize.py       timing, pointer paths, typing rhythm
  progress.py       run event bus + cooperative stop signal
  platforms/
    base.py         adapter contract, selector resolution
    flipkart.py     sequential per-item flow
    amazon.py       batch detection + sequential fallback
  selectors/*.yaml  all selectors, as ordered candidate lists
  webapp/
    server.py       FastAPI control panel; runs the agent on a worker thread
    static/         single-page UI, no build step
tests/              94 tests, run against the real dataset's cell contents
```

## Known limits

- **Selectors are unverified against the live DOM.** See point 1 above.
- **The eligibility check is a pre-filter, not the authority.** It only rejects
  items that are clearly expired, and defers to the platform whenever a date is
  approximate or missing.
- **Amazon is untested end-to-end** — the dataset is entirely Flipkart. The batch
  path is built to the documented flow and needs one supervised run.
- **Refund routing is left at the platform default.** The agent will not choose a
  bank account; that is a money decision for a human.
