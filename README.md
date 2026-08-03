# Charity Donor Outreach

An AI agent skill that turns a donor CSV export into per-donor draft
fundraising letters, with the money maths and the safety checks done by
scripts rather than by a language model.

Built for fundraising staff at the ASPCA as a case study. The donor data in
`tests/fixtures/` is mock data.

---

## What it does

Given a donor export and a handful of campaign inputs, the skill:

1. assigns each donor a **financial tier** (from lifetime giving) and an
   **engagement status** (from last gift year),
2. computes a **recommended ask amount** from an explicit, configurable
   policy,
3. **suppresses** donors who must not be contacted and **flags** rows whose
   data can't support a letter,
4. renders **one HTML letter per mailable donor**, with donor values
   HTML-escaped in code.

Everything that could differ between two runs - arithmetic, rounding,
classification, escaping - is done in Python. The language model contributes
the campaign wording: about ten short prose blocks for an entire campaign,
whether the list has 50 donors or 50,000.

## Requirements

Python 3.10 or newer (developed and tested on 3.14). No third-party
packages, no install step. The scripts use only the standard library so they
run wherever the skill is unpacked.

## Quickstart

```bash
python scripts/compute_asks.py donors.csv asks_review.csv --campaign annual --campaign-year 2026 --channel email --review-threshold 25000
```

```bash
python scripts/render_letters.py asks_review.csv letters/ --emit-content content.json
```

Fill in `content.json` (one campaign paragraph and one tier line per
segment), then:

```bash
python scripts/render_letters.py asks_review.csv letters/ --content content.json --charity "ASPCA" --donation-url "https://www.aspca.org/donate" --signer-name "Jane Okafor" --signer-title "Director of Development"
```

Letters land in `letters/letter_<donor_id>.html`, alongside
`letters/letters_manifest.csv`.

## Repository layout

| Path | Purpose |
|---|---|
| `SKILL.md` | The skill itself: workflow and policy the assistant follows |
| `scripts/compute_asks.py` | Tiers, engagement, suppression, ask amounts |
| `scripts/render_letters.py` | Merges the review CSV and content into letters |
| `assets/letter_template.html` | Letter body; placeholders are `[NAME_IN_CAPS]` |
| `assets/default_ask_policy.json` | The tunable business rules |
| `assets/content_example.json` | Example prose blocks (not read by any script) |
| `tests/test_skill.py` | Regression suite (50 tests, stdlib `unittest`) |
| `tests/fixtures/donors_sample.csv` | 50 mock donors, the sample input |
| `tests/golden/asks_review_annual_2026.csv` | Pinned output for that input |

## Input columns

Header names are matched case-insensitively; extra columns are ignored.

**Required**

| Column | Notes |
|---|---|
| `first_name`, `last_name` | Used for salutation and duplicate detection |
| `lifetime_total` | Determines financial tier. `$1,200` and `1200` both parse |
| `largest_gift` | Base for percentage-tier asks |
| `last_gift_year` | Four-digit year; determines lapse and loyalty uplift |

**Optional**

| Column | Notes |
|---|---|
| `donor_id` | Becomes the letter filename. Falls back to `row<N>` |
| `title` | Used verbatim. Never guessed from a name |
| `region` | Passed through to the review CSV for staff use |
| `volunteer` | `yes`/`no`. Adds the volunteer bonus when enabled |
| `tier` | Advisory only. Disagreement with the computed tier blocks the row |
| `deceased`, `do_not_contact` | `yes` suppresses the donor |
| `email_permission`, `mail_permission` | Only an explicit `no` suppresses, and only for the channel selected with `--channel` |

CSV only. Spreadsheets are detected and rejected with instructions rather
than crashing - adding a spreadsheet parser would mean a third-party
dependency.

## Business rules

Defaults live in `DEFAULT_POLICY` in `scripts/compute_asks.py` and are
mirrored in `assets/default_ask_policy.json`. A test asserts the two agree,
so the documented rules cannot drift from the applied ones.

**Financial tier**, by lifetime giving:
Platinum ≥ $50,000 · Gold ≥ $10,000 · Silver ≥ $1,000 · Bronze below that.

**Engagement status**, by last gift year: *Lapsed* when the last gift is
more than three full calendar years before the campaign year (for campaign
year 2026, a last gift in 2022 or earlier); otherwise *Current*.

Tier and engagement are independent. A $145,000 donor who last gave in 2020
is Platinum **and** Lapsed - the original version of this skill treated
"Lapsed" as a tier and had no rule for which letter that donor received.

**Ask amount**

- *Lapsed*, any tier: flat $50. A lapsed donor with $10,000+ lifetime giving
  is instead routed to personal staff outreach.
- *Bronze*: flat $150. Held for review if $150 is at or above 3× their
  largest gift.
- *Platinum / Gold / Silver*: 40% / 25% / 15% of the largest gift, then, in
  this order:
  1. ×1.10 if the donor gave in the prior year,
  2. +$100 if the donor volunteers,
  3. ×1.20 for an emergency campaign,
  4. round to the nearest $50, halves rounded up, minimum $50,
  5. cap at 1× the largest gift (capped rows are held for review).

Order matters: rounding before the emergency multiplier and rounding after
it give different dollars, so the order is fixed rather than left to
interpretation.

### Changing the rules

Copy `assets/default_ask_policy.json`, edit it, and pass `--policy`. Unknown
keys and wrong types are rejected at load time, so a typo stops the run
instead of quietly mis-pricing an entire campaign.

```bash
python scripts/compute_asks.py donors.csv asks_review.csv --campaign-year 2026 --policy my_policy.json
```

The `+$100 volunteer bonus` is enabled because the client's program
specifies it. Some fundraisers consider raising an ask because someone
volunteers to be poor practice; disabling it is one line in the policy file.

## Status and flags

Every row gets one status:

| Status | Meaning |
|---|---|
| `OK` | Mailable. Letters are generated for these rows only |
| `NEEDS_REVIEW` | A human must decide before this donor is contacted |
| `SUPPRESSED` | Must not be contacted. No ask, no salutation, never rendered |

Any flag holds the row except `no_title_on_file_used_full_name`, which is
informational. That whitelist is deliberate: a flag added later blocks by
default rather than silently shipping letters.

| Flag | Meaning |
|---|---|
| `suppressed_deceased` / `suppressed_do_not_contact` | Consent or status forbids contact |
| `suppressed_no_email_permission` / `suppressed_no_mail_permission` | No permission for the selected channel |
| `tier_mismatch(file=X,computed=Y)` | The file's tier label contradicts the numbers |
| `lapsed_major_donor_personal_outreach_recommended` | Lapsed donor with $10k+ lifetime giving |
| `flat_ask_exceeds_review_multiple_of_largest_gift` | The $150 Bronze ask is large relative to their history |
| `ask_capped_at_largest_gift` | Modifiers pushed the ask past their biggest-ever gift |
| `large_ask_review` | Ask exceeds `--review-threshold` |
| `missing_name`, `missing_lifetime_total`, `missing_largest_gift` | Required data absent |
| `negative_lifetime_total`, `negative_largest_gift` | Refund or reversal artefacts |
| `non_finite_lifetime_total`, `non_finite_largest_gift` | `Infinity` or `NaN` in a money column |
| `missing_or_invalid_last_gift_year`, `last_gift_year_after_campaign_year`, `implausible_last_gift_year` | Unusable gift date |
| `duplicate_donor_name`, `duplicate_donor_id` | Would produce two letters, or overwrite one |
| `cannot_compute_ask_without_largest_gift` | No basis for a percentage ask |
| `no_title_on_file_used_full_name` | Informational; full-name salutation used |

## Command reference

### `compute_asks.py`

```
python scripts/compute_asks.py <donors.csv> <output.csv> [options]
```

| Option | Default | Purpose |
|---|---|---|
| `--campaign` | `annual` | `emergency`, `annual`, `capital`, or `event` |
| `--campaign-year` | current year | Reference year for lapse and loyalty rules. Always pass explicitly |
| `--channel` | `email` | Which permission column suppresses a donor |
| `--review-threshold` | none | Flag asks above this amount. Recommended on every run |
| `--policy` | built-in | JSON file overriding the default rules |
| `--allow-reprocessed` | off | Permit an input that looks like a prior output |

### `render_letters.py`

```
python scripts/render_letters.py <review.csv> <output_dir> [options]
```

| Option | Purpose |
|---|---|
| `--emit-content PATH` | Write an empty content scaffold for the segments present, then exit |
| `--content PATH` | JSON file of campaign paragraphs and tier lines |
| `--charity`, `--donation-url`, `--signer-name`, `--signer-title` | Campaign inputs. All required to render |
| `--template PATH` | Defaults to the bundled `assets/letter_template.html` |
| `--date YYYY-MM-DD` | Letter date. Defaults to today |
| `--limit N` | Render at most N letters, for previews. Must be 1 or greater |
| `--include-flagged` | Also render NEEDS_REVIEW rows, after staff have reviewed them |
| `--force` | Clear the previous run's letters and manifest, then render |

The closing call to action is not an option: it follows the `channel`
column recorded in the review CSV, so a printed letter never tells the
donor to hit reply. A review file containing more than one channel is
refused, since those rows had different suppression rules applied.

Both scripts exit `0` on success, `1` on a fatal input or config error
(always with a single `ERROR: ...` line, never a traceback), and `2` on bad
CLI usage.

## Safety properties

These are enforced in code, not left to the model:

- **Suppressed donors are never rendered.** No flag overrides this.
- **Donor values are HTML-escaped.** A donor named `O'Brien & Sons` renders
  correctly; a cell containing `<script>` renders inert.
- **`donor_id` is sanitised before it becomes a filename**, so a crafted ID
  can't write outside the output directory.
- **Donor names never appear in filenames or the manifest.** Filenames
  travel further than file contents.
- **A leftover `[PLACEHOLDER]` is a fatal error**, not a letter mailed with a
  literal bracket in it.
- **Existing managed output needs `--force`**, and `--force` clears the
  previous run rather than writing over part of it, so a shorter second run
  can't leave another campaign's letters behind. Every letter is preflighted
  before the clear, so input, content and template failures leave the last
  good campaign intact.
- **Filenames are resolved for the whole batch before anything is written**,
  and compared case-insensitively, so `D1` and `d1` are refused instead of
  one silently overwriting the other on Windows or macOS.

## Tests

```bash
python -m unittest discover -s tests -v
```

63 tests, no dependencies. They cover the business rules, the refusal paths,
and every defect found in review so far - banker's rounding on ties, negative
and non-finite amounts producing confident asks, a capped ask escaping the
review gate, channel-blind suppression and an email-only call to action,
partial policy overrides crashing mid-run, `--force` leaving stale letters
behind or clearing good output before a template failure, case-only donor ID
collisions, nested policy-key typos, and an unvalidated `--limit`.

The golden-file test pins the full 50-donor output. Any change to a rule
shows up as a reviewable diff rather than a silent shift in what donors get
asked for. When a rule change is intentional, regenerate it:

```bash
python scripts/compute_asks.py tests/fixtures/donors_sample.csv tests/golden/asks_review_annual_2026.csv --campaign annual --campaign-year 2026
```

## Installing as a skill

Drop the directory wherever your agent looks for skills. The folder name must
match the `name` field in `SKILL.md`'s frontmatter, which cloning this
repository gives you already.

Nothing here is tied to a particular assistant: `SKILL.md` is plain Markdown
with YAML frontmatter, and the two scripts are ordinary Python with a
command-line interface.

## Design notes

**Why the donor data isn't in the skill.** The original version embedded all
50 donors' giving histories in `SKILL.md` while also telling the assistant to
read the uploaded file - two sources of truth, guaranteed to diverge. It also
put donor PII into every conversation and capped the skill at whatever fits
in a prompt.

**Why the maths is a script.** A language model doing seven-step arithmetic
in its head gives different answers on different runs and leaves the charity
no audit trail. Here, any ask can be traced to the policy that produced it.

Partial `tier_rates` and `flat_asks` objects merge over the defaults. Tier
names inside those objects are validated too, so a typo such as `Goold` fails
loudly instead of being silently ignored.

**Why the letters are a script too.** Making the numbers deterministic while
the assistant hand-writes each letter solves half the problem. Hand-written
letters don't scale past a few dozen donors, aren't reproducible, and make
HTML-escaping an instruction the model may skip. Splitting the work - the
model writes ~10 prose blocks, the script stamps them into N letters - keeps
the writing where judgement helps and the repetition where it doesn't.

**Why bad data blocks instead of being guessed.** The original said "make
reasonable assumptions and proceed," which for a fundraising letter means
inventing financial history. Every gap is now a visible line item for staff.
