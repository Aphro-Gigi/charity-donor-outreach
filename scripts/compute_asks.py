#!/usr/bin/env python3
"""
compute_asks.py - deterministic tier/engagement assignment, suppression
checks, and ask-amount calculation for donor outreach.

WHY THIS IS A SCRIPT AND NOT PROSE IN THE SKILL
-----------------------------------------------
Everything in this file is arithmetic and rule-matching. Asking a language
model to do it in its head produces different answers on different runs and
gives the charity no audit trail. Running it here means the same input file
always produces the same asks, and any ask can be traced back to the policy
that generated it.

USAGE
-----
    python compute_asks.py <donors.csv> <output.csv>
        [--campaign emergency|annual|capital|event]   (default: annual)
        [--campaign-year YYYY]   Reference year for lapse/loyalty rules.
                                 Defaults to the current year, but ALWAYS
                                 pass it explicitly: a run in December and
                                 the same run in January would otherwise
                                 disagree about who is lapsed.
        [--channel email|mail]   Which permission column gates contact
                                 (default: email).
        [--review-threshold N]   Flag any ask above N for extra review.
                                 Recommended on every run; without it
                                 nothing catches an implausibly large ask.
        [--policy policy.json]   Override DEFAULT_POLICY (rates, flats,
                                 modifiers, rounding) without editing code.
                                 See assets/default_ask_policy.json.
        [--allow-reprocessed]    Permit an input that looks like a prior
                                 output of this script (normally refused).

Exit codes: 0 = success, 1 = fatal input/config error, 2 = bad CLI usage.

INPUT COLUMNS (header names are matched case-insensitively; extras ignored)
--------------------------------------------------------------------------
    Required: first_name, last_name, lifetime_total, largest_gift,
              last_gift_year
    Optional: donor_id, title, region, volunteer (yes/no),
              tier (compared against the computed tier; the computed tier
                    wins and any disagreement is flagged),
              do_not_contact (yes/no), deceased (yes/no),
              email_permission / mail_permission
                   (yes/no; suppresses only when explicitly "no", and only
                    for the channel selected with --channel)

CSV only. Spreadsheets (.xlsx/.xls/.ods) are detected and rejected with
instructions rather than crashing - adding a spreadsheet parser would mean
a third-party dependency, and this script is deliberately stdlib-only so it
runs anywhere with no install step.

BUSINESS RULES (single source of truth - keep SKILL.md consistent)
------------------------------------------------------------------
    Engagement status (evaluated separately from financial tier, because a
    donor can be both a major donor and lapsed; the original skill treated
    "Lapsed" as a tier and had no precedence rule):
        Lapsed  : last gift more than 3 full calendar years before the
                  campaign year (campaign year 2026 -> last gift <= 2022)
        Current : otherwise
    Financial tier (by lifetime giving):
        Platinum >= 50,000 | Gold >= 10,000 | Silver >= 1,000 | Bronze < 1,000
    Ask amount:
        Lapsed (any tier): flat $50, no modifiers.
          Exception: lapsed donor with lifetime >= 10,000 -> NEEDS_REVIEW,
          flagged for personal staff outreach instead of a form letter.
        Platinum 40% / Gold 25% / Silver 15% of largest gift, then in order:
            x1.10 if the donor gave in (campaign year - 1)
            +$100 if volunteer   [the client's stated rule; disputable -
                                  see the note in SKILL.md]
            x1.20 if campaign == emergency
            round to the nearest $50, halves rounded UP (min $50)
            cap at 1x largest gift (flagged, and held for review)
        Bronze: flat $150, no modifiers.
          Exception: if $150 is at or above 3x the largest gift, the row is
          held for review (asking a $10 donor for $150 needs a human).
    Duplicates: same first+last name, or a repeated donor_id -> both rows
    flagged and held.

Rows with problems are NEVER silently guessed. Data gaps are flagged and,
where an ask cannot be computed, left blank with status=NEEDS_REVIEW.
Suppressed donors (do-not-contact, deceased, no channel permission) get
status=SUPPRESSED and no ask.
"""

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, NoReturn, TypedDict, cast

VALID_CAMPAIGNS = {"emergency", "annual", "capital", "event"}

# Which optional permission column gates each delivery channel. Suppressing
# on email permission for a printed mail campaign would silently drop valid
# recipients, so the channel has to be explicit.
CHANNEL_PERMISSION_COLUMN = {"email": "email_permission", "mail": "mail_permission"}

# Flags that annotate a row without blocking it. Everything else blocks.
#
# This is deliberately a whitelist rather than a list of blocking prefixes:
# a new flag added later is held for review by default. The reverse (block
# only known-bad flags) means a forgotten entry silently ships letters.
NON_BLOCKING_FLAGS = {"no_title_on_file_used_full_name"}

# Gift years before this are treated as data corruption rather than history.
MIN_PLAUSIBLE_GIFT_YEAR = 1900

# Tiers whose ask is a percentage of the largest gift, and so must have a
# rate; and tiers whose ask is a flat amount. A policy override that drops
# any of these would fail on the first donor who lands in that tier, so the
# absence is caught at load time instead.
REQUIRED_TIER_RATES = ("Platinum", "Gold", "Silver")
REQUIRED_FLAT_ASKS = ("Bronze", "Lapsed")
POLICY_MAP_KEYS = {
    "tier_rates": frozenset(REQUIRED_TIER_RATES),
    "flat_asks": frozenset(REQUIRED_FLAT_ASKS),
}

# Byte signatures for file types users commonly hand over instead of a CSV.
_NON_CSV_SIGNATURES: list[tuple[bytes, str]] = [
    (b"PK\x03\x04", "an .xlsx / .ods spreadsheet (or another ZIP archive)"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "a legacy .xls / .doc file"),
    (b"%PDF", "a PDF"),
    (b"\x1f\x8b", "a gzip archive"),
]


def fail(message: str) -> NoReturn:
    """Exit(1) with the script's standard error prefix.

    Every fatal path goes through here so the caller (and the assistant
    driving this skill) can rely on one recognisable format instead of
    sometimes getting a Python traceback.
    """
    sys.exit(f"ERROR: {message}")


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class AskPolicy(TypedDict):
    """The tunable business rules. Mirrored in assets/default_ask_policy.json."""

    tier_rates: dict[str, float]
    flat_asks: dict[str, int]
    loyalty_uplift_enabled: bool
    loyalty_uplift_rate: float
    volunteer_bonus_enabled: bool
    volunteer_bonus_amount: int
    emergency_multiplier_enabled: bool
    emergency_multiplier: float
    rounding_increment: int
    cap_at_largest_gift: bool
    flat_ask_review_multiple: float
    lapsed_major_lifetime_threshold: int


DEFAULT_POLICY: AskPolicy = {
    "tier_rates": {"Platinum": 0.40, "Gold": 0.25, "Silver": 0.15},
    "flat_asks": {"Bronze": 150, "Lapsed": 50},
    "loyalty_uplift_enabled": True,
    "loyalty_uplift_rate": 0.10,
    "volunteer_bonus_enabled": True,  # client's stated rule; disputed practice
    "volunteer_bonus_amount": 100,
    "emergency_multiplier_enabled": True,
    "emergency_multiplier": 1.20,
    "rounding_increment": 50,
    "cap_at_largest_gift": True,
    "flat_ask_review_multiple": 3.0,
    "lapsed_major_lifetime_threshold": 10000,
}

# Expected shape of each policy key, used to validate an override file.
# A table beats a per-key if/elif chain: adding a rule means adding one line
# here, and the validation cannot drift from the key it validates.
_POLICY_KINDS: dict[str, str] = {
    "tier_rates": "number_map",
    "flat_asks": "number_map",
    "loyalty_uplift_enabled": "bool",
    "loyalty_uplift_rate": "number",
    "volunteer_bonus_enabled": "bool",
    "volunteer_bonus_amount": "number",
    "emergency_multiplier_enabled": "bool",
    "emergency_multiplier": "number",
    "rounding_increment": "number",
    "cap_at_largest_gift": "bool",
    "flat_ask_review_multiple": "number",
    "lapsed_major_lifetime_threshold": "number",
}


def _is_nonneg_number(value: object) -> bool:
    """True for a finite, non-negative int/float.

    Rejects bools (bool is an int in Python) and rejects Infinity/NaN, which
    Python's json module accepts as literals even though standard JSON does
    not - an infinite multiplier would otherwise reach the ask calculation.
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def load_policy(path: str | None) -> AskPolicy:
    """Merge a JSON policy file over the defaults, validating as we go.

    Validation is strict and happens at load time: an unknown key or a value
    of the wrong type stops the run. A silently ignored typo in a policy file
    would mean every letter in the campaign carries the wrong ask.
    """
    policy = cast(AskPolicy, dict(DEFAULT_POLICY))
    # The two nested dicts must be copied, or an override would mutate
    # DEFAULT_POLICY itself and leak into later calls within one process.
    policy["tier_rates"] = dict(DEFAULT_POLICY["tier_rates"])
    policy["flat_asks"] = dict(DEFAULT_POLICY["flat_asks"])

    if not path:
        return policy

    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        fail(f"policy file not found: {path}")
    except json.JSONDecodeError as exc:
        fail(f"policy file {path} is not valid JSON: {exc}")
    except OSError as exc:
        fail(f"could not read policy file {path}: {exc}")

    if not isinstance(user, dict):
        fail("policy JSON must be an object, e.g. {\"rounding_increment\": 25}")
    user_policy = cast(dict[str, Any], user)

    unknown = set(user_policy) - set(_POLICY_KINDS)
    if unknown:
        fail(
            f"unknown policy keys: {sorted(unknown)}. "
            f"Valid keys: {sorted(_POLICY_KINDS)}"
        )

    for key, value in user_policy.items():
        kind = _POLICY_KINDS[key]
        default = DEFAULT_POLICY[key]  # type: ignore[literal-required]
        ok = False
        if kind == "bool":
            ok = isinstance(value, bool)
        elif kind == "number":
            ok = _is_nonneg_number(value)
        elif kind == "number_map":
            ok = (
                isinstance(value, dict)
                and bool(value)
                and all(_is_nonneg_number(v) for v in cast(dict[str, object], value).values())
            )
        if not ok:
            fail(
                f"policy key '{key}' has invalid value {value!r} "
                f"(expected something shaped like {default!r})"
            )
        if kind == "number_map":
            value_map = cast(dict[str, float], value)
            unknown_nested = set(value_map) - POLICY_MAP_KEYS[key]
            if unknown_nested:
                fail(
                    f"policy key '{key}' contains unknown tier(s): "
                    f"{sorted(unknown_nested)}. Valid tiers: "
                    f"{sorted(POLICY_MAP_KEYS[key])}"
                )
            # Merge rather than replace. Overriding one rate is the common
            # case, and a wholesale replacement would silently drop the tiers
            # the file does not mention - which then fails mid-run with a
            # KeyError on the first donor in a missing tier.
            merged = dict(policy[key])  # type: ignore[literal-required]
            merged.update(value_map)
            policy[key] = merged  # type: ignore[literal-required]
        else:
            policy[key] = value  # type: ignore[literal-required]

    # Cross-field checks that a per-key type check cannot catch.
    if policy["rounding_increment"] <= 0:
        fail("rounding_increment must be greater than zero")
    for tier in REQUIRED_TIER_RATES:
        if tier not in policy["tier_rates"]:
            fail(f"tier_rates must define '{tier}'")
    for tier in REQUIRED_FLAT_ASKS:
        if tier not in policy["flat_asks"]:
            fail(f"flat_asks must define '{tier}'")

    return policy


# --------------------------------------------------------------------------
# Field parsing
# --------------------------------------------------------------------------


def parse_money(raw: object) -> float | None:
    """Parse a currency cell. Returns None when the cell is blank or unparseable.

    Accepts the shapes CRM exports actually produce: "$1,200", "1200",
    "1,200.00", " 1200 ". Returns None rather than guessing, so the caller
    can flag the row instead of inventing a number.
    """
    if raw is None:
        return None
    s = str(raw).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_bool(raw: object) -> bool:
    """True only for an explicit affirmative. Blank/unknown reads as False."""
    return str(raw or "").strip().lower() in {"yes", "y", "true", "1"}


def explicit_no(raw: object) -> bool:
    """True only for an explicit negative.

    Used for permission columns, where blank means "not recorded" and must
    not be read as consent withdrawal (that would suppress the whole file
    for charities that leave the column empty).
    """
    return str(raw or "").strip().lower() in {"no", "n", "false", "0"}


# --------------------------------------------------------------------------
# Money maths
# --------------------------------------------------------------------------


def round_to_increment(value: float, increment: int) -> int:
    """Round to the nearest `increment`, with halves rounded up.

    Python's built-in round() uses banker's rounding, so round(125/50)*50
    gives $100 while round(175/50)*50 gives $200 - inconsistent tie-breaking
    that no fundraiser would predict from "round to the nearest $50".
    Decimal with ROUND_HALF_UP makes ties always go up: 125 -> 150.
    """
    quotient = Decimal(str(value)) / Decimal(increment)
    return int(quotient.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * increment


def financial_tier(lifetime: float) -> str:
    """Tier from lifetime giving alone. Engagement is computed separately."""
    if lifetime >= 50_000:
        return "Platinum"
    if lifetime >= 10_000:
        return "Gold"
    if lifetime >= 1_000:
        return "Silver"
    return "Bronze"


def compute_ask(
    tier: str,
    engagement: str,
    largest: float | None,
    last_gift_year: int | None,
    volunteer: bool,
    campaign: str,
    campaign_year: int,
    flags: list[str],
    policy: AskPolicy,
) -> int:
    """Return the recommended ask in whole dollars, appending any flags raised.

    Modifier order is fixed and explicit (rate -> loyalty -> volunteer ->
    emergency -> round -> cap). The original skill left the order ambiguous,
    which changes the answer: rounding before the emergency multiplier and
    rounding after it give different dollars.
    """
    increment = policy["rounding_increment"]

    # Lapsed donors get a small re-engagement ask regardless of tier; the
    # point is to restart the relationship, not to maximise this gift.
    if engagement == "Lapsed":
        return policy["flat_asks"]["Lapsed"]

    # Bronze is a flat ask - percentages of a tiny largest gift produce
    # asks too small to be worth mailing.
    if tier == "Bronze":
        flat = policy["flat_asks"]["Bronze"]
        if largest is not None and flat >= policy["flat_ask_review_multiple"] * largest:
            flags.append("flat_ask_exceeds_review_multiple_of_largest_gift")
        return flat

    if largest is None:
        # Guarded by the caller; this is a programming-error backstop.
        raise ValueError("largest gift is required for non-lapsed, non-Bronze asks")

    ask = largest * policy["tier_rates"][tier]
    if policy["loyalty_uplift_enabled"] and last_gift_year == campaign_year - 1:
        ask *= 1 + policy["loyalty_uplift_rate"]
    if policy["volunteer_bonus_enabled"] and volunteer:
        ask += policy["volunteer_bonus_amount"]
    if policy["emergency_multiplier_enabled"] and campaign == "emergency":
        ask *= policy["emergency_multiplier"]

    ask = max(increment, round_to_increment(ask, increment))

    # Never ask for more than the donor has ever given at once. If the
    # modifiers pushed past that, the row is flagged and held: the formula
    # producing an ask larger than the donor's biggest-ever gift means the
    # inputs deserve a human look, not a quietly trimmed number.
    if policy["cap_at_largest_gift"] and ask > largest:
        flags.append("ask_capped_at_largest_gift")
        ask = max(increment, int(largest // increment) * increment)
    return ask


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------


def reject_non_csv(path: str) -> None:
    """Stop with a useful message if `path` is obviously not a CSV.

    Without this, handing the script an .xlsx produces a raw
    UnicodeDecodeError traceback that tells a fundraiser nothing.
    """
    try:
        with open(path, "rb") as f:
            peek = f.read(8192)
    except FileNotFoundError:
        fail(f"input file not found: {path}")
    except OSError as exc:
        fail(f"could not read input file {path}: {exc}")

    for signature, description in _NON_CSV_SIGNATURES:
        if peek.startswith(signature):
            fail(
                f"{path} looks like {description}, not a CSV. This skill reads "
                f"CSV only. In Excel: File > Save As > 'CSV UTF-8 (Comma "
                f"delimited)', then re-run on the .csv file."
            )
    if b"\x00" in peek:
        fail(
            f"{path} contains binary data and is not a readable CSV. "
            f"Re-export it from your CRM as CSV UTF-8."
        )


def sniff_delimiter(header_line: str) -> str:
    """Detect the column separator from the header row.

    Excel on a European locale exports semicolon-separated files by default,
    which would otherwise look like a file with one strangely-named column.
    Falls back to comma when detection is inconclusive (e.g. a single-column
    file), which is the correct default.
    """
    try:
        return csv.Sniffer().sniff(header_line, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def read_donor_file(
    path: str, allow_reprocessed: bool
) -> tuple[list[dict[str, str]], dict[str, str], str]:
    """Read and structurally validate the donor CSV.

    Returns (rows, fieldmap, delimiter), where fieldmap maps a lowercase
    canonical column name to the header exactly as it appears in the file.
    """
    reject_non_csv(path)

    try:
        # utf-8-sig transparently strips the BOM Excel writes on Windows.
        with open(path, newline="", encoding="utf-8-sig") as f:
            first_line = f.readline()
            if not first_line.strip():
                fail("input CSV has no header row")
            delimiter = sniff_delimiter(first_line)
            f.seek(0)

            reader = csv.DictReader(f, delimiter=delimiter)
            if reader.fieldnames is None:
                fail("input CSV has no header row")

            fieldmap = {name.strip().lower(): name for name in reader.fieldnames}

            required = {
                "first_name",
                "last_name",
                "lifetime_total",
                "largest_gift",
                "last_gift_year",
            }
            missing = required - set(fieldmap)
            if missing:
                hint = ""
                if len(reader.fieldnames) == 1:
                    hint = (
                        " The file appears to have a single column - it may use "
                        "a delimiter this script could not detect. Re-export it "
                        "as comma-separated CSV."
                    )
                fail(
                    f"input CSV is missing required columns: {sorted(missing)}. "
                    f"Found: {reader.fieldnames}.{hint}"
                )

            # Guard against re-running on a prior output of this script. The
            # output's `tier` column holds COMPUTED tiers, so a second pass
            # compares computed against computed and every tier_mismatch flag
            # silently disappears - the run looks cleaner while hiding the
            # very data errors it exists to surface.
            output_signature = {"status", "flags", "engagement", "salutation", "ask_amount"}
            if output_signature <= set(fieldmap) and not allow_reprocessed:
                fail(
                    "this file looks like a prior output of this script (it "
                    f"contains {sorted(output_signature)}). Re-running on an "
                    "output file clears tier_mismatch flags, because the tier "
                    "column already holds computed values. Use the original CRM "
                    "export instead, or pass --allow-reprocessed if intentional."
                )

            rows = list(reader)
    except UnicodeDecodeError:
        fail(
            f"{path} is not valid UTF-8 text. Re-export it from your CRM or "
            f"Excel as 'CSV UTF-8 (Comma delimited)'."
        )

    if not rows:
        fail("input CSV has a header but no donor rows")
    return rows, fieldmap, delimiter


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Compute donor tiers, engagement, suppression and ask amounts."
    )
    ap.add_argument("input_csv", help="Donor export from the CRM (CSV)")
    ap.add_argument("output_csv", help="Review CSV to write")
    ap.add_argument("--campaign", default="annual", choices=sorted(VALID_CAMPAIGNS))
    ap.add_argument(
        "--campaign-year",
        type=int,
        default=date.today().year,
        help="Reference year for lapse/loyalty rules (pass explicitly for reproducibility)",
    )
    ap.add_argument(
        "--channel",
        default="email",
        choices=sorted(CHANNEL_PERMISSION_COLUMN),
        help="Delivery channel; selects which permission column suppresses a donor",
    )
    ap.add_argument(
        "--review-threshold",
        type=float,
        default=None,
        help="Flag asks above this amount for extra review (recommended)",
    )
    ap.add_argument("--policy", default=None, help="JSON file overriding the default ask policy")
    ap.add_argument(
        "--allow-reprocessed",
        action="store_true",
        help="Permit an input that looks like a prior output of this script "
        "(normally refused, because re-running on an output silently clears "
        "tier_mismatch flags)",
    )
    return ap


def main() -> None:
    args = build_arg_parser().parse_args()
    policy = load_policy(args.policy)
    permission_column = CHANNEL_PERMISSION_COLUMN[args.channel]

    rows, fieldmap, delimiter = read_donor_file(args.input_csv, args.allow_reprocessed)

    def get(row: dict[str, str], key: str) -> str | None:
        """Read a canonical column from a row, tolerating header case/spacing."""
        col = fieldmap.get(key)
        return row.get(col) if col else None

    # Duplicate detection needs a full pass before per-row processing, so
    # that BOTH copies of a duplicate are flagged rather than just the second.
    name_counts = Counter(
        (
            (get(r, "first_name") or "").strip().lower(),
            (get(r, "last_name") or "").strip().lower(),
        )
        for r in rows
    )
    # A repeated donor_id is a separate problem: letters are written to
    # letter_<donor_id>.html, so duplicates would overwrite each other and
    # one donor would silently receive nothing. Compared case-insensitively
    # because Windows and macOS filesystems are - "D1" and "d1" are distinct
    # IDs that resolve to the same file.
    id_counts = Counter(
        (get(r, "donor_id") or "").strip().casefold()
        for r in rows
        if (get(r, "donor_id") or "").strip()
    )

    out_rows: list[dict[str, str | int]] = []
    status_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    segment_counts: Counter[str] = Counter()

    for line_number, row in enumerate(rows, start=2):  # header occupies line 1
        flags: list[str] = []
        donor_id = (get(row, "donor_id") or "").strip()
        first = (get(row, "first_name") or "").strip()
        last = (get(row, "last_name") or "").strip()
        title = (get(row, "title") or "").strip()
        lifetime = parse_money(get(row, "lifetime_total"))
        largest = parse_money(get(row, "largest_gift"))
        raw_year = str(get(row, "last_gift_year") or "").strip()
        last_year = int(raw_year) if raw_year.isdigit() else None
        volunteer = parse_bool(get(row, "volunteer"))
        stated_tier = (get(row, "tier") or "").strip().title()

        # --- suppression (checked first: a suppressed donor needs no ask) ---
        suppressed = False
        if parse_bool(get(row, "deceased")):
            flags.append("suppressed_deceased")
            suppressed = True
        if parse_bool(get(row, "do_not_contact")):
            flags.append("suppressed_do_not_contact")
            suppressed = True
        if explicit_no(get(row, permission_column)):
            flags.append(f"suppressed_no_{args.channel}_permission")
            suppressed = True

        # --- data validation ---
        if not first or not last:
            flags.append("missing_name")
        if first and last and name_counts[(first.lower(), last.lower())] > 1:
            flags.append("duplicate_donor_name")
        if donor_id and id_counts[donor_id.casefold()] > 1:
            flags.append("duplicate_donor_id")
        if lifetime is None:
            flags.append("missing_lifetime_total")
        elif not math.isfinite(lifetime):
            # "Infinity" and "NaN" parse as floats. Unflagged, Infinity
            # reaches the letter as a lifetime total of "$inf" and NaN
            # silently fails every comparison, leaving a held row with no
            # explanation of what is wrong with it.
            flags.append("non_finite_lifetime_total")
        elif lifetime < 0:
            # A negative total is a refund/reversal artefact, not a donor
            # who has given negative money. Left unflagged it can produce a
            # confident ask from corrupt data.
            flags.append("negative_lifetime_total")
        if largest is None:
            flags.append("missing_largest_gift")
        elif not math.isfinite(largest):
            flags.append("non_finite_largest_gift")
        elif largest < 0:
            flags.append("negative_largest_gift")
        if last_year is None:
            flags.append("missing_or_invalid_last_gift_year")
        elif last_year > args.campaign_year:
            flags.append("last_gift_year_after_campaign_year")
        elif last_year < MIN_PLAUSIBLE_GIFT_YEAR:
            flags.append("implausible_last_gift_year")

        # --- tier, engagement, ask ---
        tier: str | None = None
        engagement: str | None = None
        ask: int | None = None

        # Usable means present, finite and non-negative. Anything else is
        # corrupt data that has already been flagged above.
        lifetime_usable = lifetime is not None and math.isfinite(lifetime) and lifetime >= 0

        if not suppressed and lifetime_usable:
            tier = financial_tier(lifetime)
            engagement = (
                "Lapsed"
                if last_year is not None and (args.campaign_year - last_year) > 3
                else "Current"
            )

            # The file's own tier label is advisory. When it disagrees with
            # the numbers the row is held, because tier drives tone,
            # salutation and offer - the wrong tier is a wrong letter, not
            # just a wrong label. "Lapsed"/"Unknown" in a tier column are
            # engagement states, not financial tiers, so they never mismatch.
            if stated_tier and stated_tier not in ("Lapsed", "Unknown") and stated_tier != tier:
                flags.append(f"tier_mismatch(file={stated_tier},computed={tier})")

            if (
                engagement == "Lapsed"
                and lifetime >= policy["lapsed_major_lifetime_threshold"]
            ):
                # A $50 form letter is the wrong response to a lapsed major
                # donor; this belongs to a human with a phone.
                flags.append("lapsed_major_donor_personal_outreach_recommended")

            # A largest-gift figure is required for every Current-donor ask:
            # the percentage tiers multiply it, and Bronze compares its flat
            # ask against it. A negative or non-finite value is corrupt
            # rather than small, and is flagged above; either way no ask is
            # computed from it.
            largest_usable = largest is not None and math.isfinite(largest) and largest >= 0

            if engagement == "Lapsed":
                # Flat re-engagement ask - needs no largest-gift figure.
                ask = compute_ask(
                    tier, engagement, largest, last_year, volunteer,
                    args.campaign, args.campaign_year, flags, policy,
                )
            elif largest is None:
                flags.append("cannot_compute_ask_without_largest_gift")
            elif largest_usable:
                ask = compute_ask(
                    tier, engagement, largest, last_year, volunteer,
                    args.campaign, args.campaign_year, flags, policy,
                )

            if ask is not None and args.review_threshold is not None and ask > args.review_threshold:
                flags.append("large_ask_review")

        # Segment drives letter content downstream: Lapsed overrides tier,
        # matching the tone rules in SKILL.md.
        segment = "Lapsed" if engagement == "Lapsed" else (tier or "")

        # --- salutation: never guess gender or honorific from a name ---
        if suppressed:
            # Left blank so a suppressed row can never be mistaken for a
            # mailable one further down the pipeline.
            salutation = ""
        elif engagement == "Lapsed":
            salutation = f"We've missed you, {first}!"
        elif tier in ("Platinum", "Gold"):
            salutation = f"Dear {title} {last}," if title else f"Dear {first} {last},"
            if not title:
                flags.append("no_title_on_file_used_full_name")
        else:
            salutation = f"Hi {first}," if first else ""

        # --- status ---
        # Anything flagged blocks the letter unless the flag is explicitly
        # listed as informational. Flag names may carry a "(detail)" suffix,
        # so compare on the part before the parenthesis.
        blocking = [f for f in flags if f.split("(", 1)[0] not in NON_BLOCKING_FLAGS]
        if suppressed:
            status = "SUPPRESSED"
        elif blocking or ask is None:
            status = "NEEDS_REVIEW"
        else:
            status = "OK"

        status_counts[status] += 1
        flag_counts.update(f.split("(", 1)[0] for f in flags)
        if status == "OK":
            segment_counts[segment] += 1

        out_rows.append(
            {
                "row": line_number,
                # Fall back to the line number so every row has a stable,
                # non-identifying handle for its output filename.
                "donor_id": donor_id or f"row{line_number}",
                "first_name": first,
                "last_name": last,
                "region": (get(row, "region") or "").strip(),
                "tier": tier or "",
                "engagement": engagement or "",
                "segment": segment,
                "salutation": salutation,
                # Raw columns sort and pivot correctly in a spreadsheet;
                # *_display columns are what gets merged into letters, so
                # money is formatted once, here, rather than by the model.
                "lifetime_total": f"{lifetime:.0f}" if lifetime is not None else "",
                "lifetime_total_display": f"{lifetime:,.0f}" if lifetime is not None else "",
                "largest_gift": f"{largest:.0f}" if largest is not None else "",
                "largest_gift_display": f"{largest:,.0f}" if largest is not None else "",
                "last_gift_year": raw_year,
                "volunteer": "Yes" if volunteer else "No",
                "ask_amount": ask if ask is not None else "",
                "ask_amount_display": f"{ask:,.0f}" if ask is not None else "",
                # Recorded per row so the review file is self-describing and
                # the renderer cannot pick a call to action for a different
                # channel than the one these suppression rules were applied for.
                "channel": args.channel,
                "status": status,
                "flags": "; ".join(flags),
            }
        )

    try:
        with open(args.output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            writer.writeheader()
            writer.writerows(out_rows)
    except OSError as exc:
        fail(f"could not write {args.output_csv}: {exc}")

    # --- summary: this is what the assistant reports back to the user ---
    delimiter_note = "" if delimiter == "," else f", delimiter={delimiter!r}"
    print(
        f"Processed {len(out_rows)} donors | campaign={args.campaign}, "
        f"reference year={args.campaign_year}, channel={args.channel}{delimiter_note}"
    )
    print(
        f"  OK={status_counts['OK']}  NEEDS_REVIEW={status_counts['NEEDS_REVIEW']}  "
        f"SUPPRESSED={status_counts['SUPPRESSED']}"
    )
    if segment_counts:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(segment_counts.items()))
        print(f"  Mailable segments: {breakdown}")
    if flag_counts:
        print("  Flags raised:")
        for flag, count in sorted(flag_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {count:>6}  {flag}")
    print(f"Review file written to {args.output_csv}")
    if status_counts["NEEDS_REVIEW"] or status_counts["SUPPRESSED"]:
        print(
            "Generate letters ONLY for status=OK rows. NEEDS_REVIEW rows require "
            "a human decision; SUPPRESSED rows must not be contacted."
        )


if __name__ == "__main__":
    main()
