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

**3. Login: the agent drives the form, you supply the code.**
Give it the mobile number with `--phone` and it fills the login form and presses
*Request OTP* itself. The code cannot be automated — it is delivered out of band
to the account holder — so the agent prompts you for it at the terminal, then
types and submits it.

Omit the number and it falls back to letting you sign in entirely by hand; it also
falls back automatically if the login form can't be located, so a changed login
page never blocks a run. Because the browser profile persists, sign-in is
once-per-profile rather than once-per-run.

Codes are held in memory only for the moment between entry and submission. They
are never logged, written to the workbook, or persisted.

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

The spec fixes `Return status` to **Placed / Failed / Out of window**, which can't
express everything a platform actually reports. So that column holds only those
three values, and a `Detail` column carries the precise state. Nothing is lost and
the column stays conformant.

| Detail | Return status | Task status |
|---|---|---|
| `Placed` | `Placed` | Done |
| `Already Cancelled & Refunded` | `Placed` | Done |
| `Out of window` | `Out of window` | Done |
| `Not yet delivered` | `Failed` | Done |
| `Support Needed` | `Failed` | **Needs human review** |
| `Item not found on order` | `Failed` | **Needs human review** |
| `Not returnable` | `Failed` | **Needs human review** |
| `Not ordered (NA)` | `Failed` | **Needs human review** |
| `Failed` | `Failed` | **Needs human review** |
| `Planned (not attempted)` | *(blank)* | Pending |

Two mappings are judgement calls worth stating:

- **`Already Cancelled & Refunded` → `Placed`.** A return and refund do exist for
  that line item; the agent simply didn't have to create them. `Failed` would be
  plainly wrong.
- **`Planned (not attempted)` → blank.** Plan-only mode attempts nothing, so
  claiming `Failed` would assert a failure that never happened. Blank, paired with
  a Task status of `Pending`, says exactly what is true.

Resume decisions read `Detail`, not `Return status` — `Support Needed` and
`Failed` both collapse to `Failed`, but only the latter should be re-attempted.

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

Each record gets its own browser tab, per the spec workflow; the tab is closed
when the record is done so a long run doesn't accumulate one tab per order. The
browser context is shared, so a new tab never means signing in again.

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

**Interrupting a run**
Ctrl-C sets a stop flag rather than unwinding immediately, so the run finishes
the line item in flight and stops at the next pause - never between clicking
Confirm and reading back the return ID. Whatever was gathered is still written
to Excel. A second Ctrl-C stops at once.

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
| `--phone N` | Mobile number for OTP sign-in. The agent fills the form and requests the code, then asks you for it. |
| `--seed N` | Seed the timing RNG for reproducible runs. |
| `--allow-quiet-hours` | Permit a run inside quiet hours. |
| `--grace-days N` | Slack before declaring an item out of window (default 1). |

---

## Layout

```
src/faym_returns/
  cli.py            entry point; dry-run by default
  orchestrator.py   run loop, planning, partial-success handling
  normalize.py      messy-cell parsing, order row -> line items
  eligibility.py    return-window pre-filter
  workbook.py       Excel read + per-line-item write-back
  browser.py        persistent Chrome session, fingerprint, challenge detection
  humanize.py       timing, pointer paths, typing rhythm
  progress.py       cooperative stop signal (Ctrl-C aborts at a safe pause)
  otp.py            one-time-code provider for the OTP sign-in
  platforms/
    base.py         adapter contract, selector resolution
    flipkart.py     sequential per-item flow
    amazon.py       batch detection + sequential fallback
  selectors/*.yaml  all selectors, as ordered candidate lists
tests/              100 tests, run against the real dataset's cell contents
```

## Known limits

- **Selectors are unverified against the live DOM.** See point 1 above.
- **The eligibility check is a pre-filter, not the authority.** It only rejects
  items that are clearly expired, and defers to the platform whenever a date is
  approximate or missing.
- **Amazon is untested end-to-end** — the dataset is entirely Flipkart. The batch
  path is built to the documented flow and needs one supervised run.
- **Refund routing is left at the platform default.** The agent will not choose a
  bank account; that is a money decision for a human. Likewise it confirms the
  pickup address already on the order rather than changing where a courier goes.
