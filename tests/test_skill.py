#!/usr/bin/env python3
"""
Regression tests for the charity-donor-outreach skill.

Run from the skill root:

    python -m unittest discover -s tests -v

or just:

    python tests/test_skill.py

Stdlib only - no pytest, no install step - so the tests run wherever the
scripts themselves run.

WHAT THESE TESTS ARE FOR
------------------------
The skill's central promise is that identical inputs produce identical
letters, and that bad data is held for a human instead of being guessed.
Both promises live in the two scripts, so both are tested here:

  * a golden-file test pins the full 50-donor output, so any change to the
    rules shows up as a reviewable diff rather than a silent shift in what
    donors get asked for;
  * targeted tests cover each rule and each refusal path, including the
    specific defects found during review (banker's rounding, negative
    amounts, a capped ask escaping review, channel-blind suppression).
"""

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# --- paths -----------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
COMPUTE = ROOT / "scripts" / "compute_asks.py"
RENDER = ROOT / "scripts" / "render_letters.py"
TEMPLATE = ROOT / "assets" / "letter_template.html"
CONTENT_EXAMPLE = ROOT / "assets" / "content_example.json"
DEFAULT_POLICY_JSON = ROOT / "assets" / "default_ask_policy.json"
FIXTURE = ROOT / "tests" / "fixtures" / "donors_sample.csv"
GOLDEN = ROOT / "tests" / "golden" / "asks_review_annual_2026.csv"

# The golden file was generated for this campaign year; the lapse rule is
# relative to it, so the year is pinned here rather than taken from today.
GOLDEN_YEAR = "2026"


def _import_compute_asks():
    """Import compute_asks.py as a module so its functions can be unit-tested."""
    spec = importlib.util.spec_from_file_location("compute_asks", COMPUTE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compute_asks = _import_compute_asks()


# --- helpers ---------------------------------------------------------------


def run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one of the skill's scripts and capture its output."""
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def write_csv(path: Path, header: list[str], rows: list[list[object]]) -> Path:
    """Write a small donor CSV for a single test case."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


DONOR_HEADER = [
    "donor_id",
    "first_name",
    "last_name",
    "lifetime_total",
    "largest_gift",
    "last_gift_year",
    "volunteer",
    "tier",
]


class SkillTestCase(unittest.TestCase):
    """Base class providing a scratch directory per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def compute(self, rows: list[list[object]], *extra: str) -> list[dict[str, str]]:
        """Run compute_asks.py over `rows` and return the parsed review CSV."""
        src = write_csv(self.tmp / "donors.csv", DONOR_HEADER, rows)
        out = self.tmp / "review.csv"
        result = run(COMPUTE, str(src), str(out), "--campaign-year", "2026", *extra)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return read_rows(out)


# ---------------------------------------------------------------------------
# Unit tests: the maths and the policy loader
# ---------------------------------------------------------------------------


class TestMoneyMaths(unittest.TestCase):
    def test_rounds_halves_up_not_to_even(self) -> None:
        """Ties must round up.

        Python's round() is banker's rounding: round(125/50)*50 gives 100
        while round(175/50)*50 gives 200. "Round to the nearest $50" has to
        mean the same thing at every tie, or two donors with comparable
        histories get inconsistent asks.
        """
        self.assertEqual(compute_asks.round_to_increment(125, 50), 150)
        self.assertEqual(compute_asks.round_to_increment(175, 50), 200)
        self.assertEqual(compute_asks.round_to_increment(225, 50), 250)
        self.assertEqual(compute_asks.round_to_increment(124.99, 50), 100)

    def test_tier_boundaries_are_inclusive_at_the_floor(self) -> None:
        self.assertEqual(compute_asks.financial_tier(50_000), "Platinum")
        self.assertEqual(compute_asks.financial_tier(49_999), "Gold")
        self.assertEqual(compute_asks.financial_tier(10_000), "Gold")
        self.assertEqual(compute_asks.financial_tier(9_999), "Silver")
        self.assertEqual(compute_asks.financial_tier(1_000), "Silver")
        self.assertEqual(compute_asks.financial_tier(999), "Bronze")
        self.assertEqual(compute_asks.financial_tier(0), "Bronze")

    def test_parse_money_handles_crm_formats_and_refuses_junk(self) -> None:
        self.assertEqual(compute_asks.parse_money("$1,200.50"), 1200.50)
        self.assertEqual(compute_asks.parse_money(" 900 "), 900.0)
        self.assertIsNone(compute_asks.parse_money(""))
        self.assertIsNone(compute_asks.parse_money("n/a"))
        self.assertIsNone(compute_asks.parse_money(None))

    def test_permission_columns_treat_blank_as_unrecorded(self) -> None:
        """Blank must not read as consent withdrawal, or a charity that
        leaves the column empty would suppress its entire file."""
        self.assertTrue(compute_asks.explicit_no("no"))
        self.assertTrue(compute_asks.explicit_no("FALSE"))
        self.assertFalse(compute_asks.explicit_no(""))
        self.assertFalse(compute_asks.explicit_no(None))


class TestPolicyLoader(SkillTestCase):
    def test_shipped_policy_asset_matches_code_defaults(self) -> None:
        """assets/default_ask_policy.json documents DEFAULT_POLICY; if the two
        drift, the documented rules stop being the applied rules."""
        with open(DEFAULT_POLICY_JSON, encoding="utf-8") as f:
            asset = json.load(f)
        self.assertEqual(asset, dict(compute_asks.DEFAULT_POLICY))

    def _policy_error(self, payload: str) -> str:
        path = self.tmp / "policy.json"
        path.write_text(payload, encoding="utf-8")
        rows = [["D1", "A", "B", 5000, 1500, 2025, "No", ""]]
        src = write_csv(self.tmp / "donors.csv", DONOR_HEADER, rows)
        result = run(COMPUTE, str(src), str(self.tmp / "o.csv"), "--policy", str(path))
        self.assertEqual(result.returncode, 1)
        return result.stderr

    def test_unknown_key_fails_loudly(self) -> None:
        # A typo that was silently ignored would put the wrong ask in every letter.
        self.assertIn("unknown policy keys", self._policy_error('{"volunter_bonus_enabled": false}'))

    def test_wrong_type_fails_loudly(self) -> None:
        self.assertIn("invalid value", self._policy_error('{"rounding_increment": "fifty"}'))

    def test_malformed_json_reports_cleanly(self) -> None:
        # Must be the script's own ERROR: line, not a Python traceback.
        message = self._policy_error("{oops")
        self.assertIn("not valid JSON", message)
        self.assertNotIn("Traceback", message)

    def test_missing_policy_file_reports_cleanly(self) -> None:
        rows = [["D1", "A", "B", 5000, 1500, 2025, "No", ""]]
        src = write_csv(self.tmp / "donors.csv", DONOR_HEADER, rows)
        result = run(COMPUTE, str(src), str(self.tmp / "o.csv"), "--policy", str(self.tmp / "gone.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("policy file not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_override_changes_the_ask(self) -> None:
        path = self.tmp / "policy.json"
        path.write_text('{"volunteer_bonus_enabled": false}', encoding="utf-8")
        rows = [["D1", "A", "B", 60000, 10000, 2023, "Yes", "Platinum"]]
        with_bonus = self.compute(rows)[0]["ask_amount"]
        without = self.compute(rows, "--policy", str(path))[0]["ask_amount"]
        self.assertEqual(with_bonus, "4100")
        self.assertEqual(without, "4000")


# ---------------------------------------------------------------------------
# compute_asks.py: rules
# ---------------------------------------------------------------------------


class TestGoldenFile(unittest.TestCase):
    def test_sample_dataset_reproduces_golden_output(self) -> None:
        """The whole 50-donor pipeline is deterministic.

        This is the test that fails if anyone changes a rule: the diff shows
        exactly which donors' asks or statuses moved.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "review.csv"
            result = run(
                COMPUTE, str(FIXTURE), str(out),
                "--campaign", "annual", "--campaign-year", GOLDEN_YEAR,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                out.read_text(encoding="utf-8").splitlines(),
                GOLDEN.read_text(encoding="utf-8").splitlines(),
                "output differs from tests/golden/asks_review_annual_2026.csv",
            )


class TestSegmentation(SkillTestCase):
    def test_lapsed_boundary_is_more_than_three_full_years(self) -> None:
        rows = [
            ["D1", "Just", "Current", 5000, 1500, 2023, "No", ""],  # 3 years back
            ["D2", "Just", "Lapsed", 5000, 1500, 2022, "No", ""],   # 4 years back
        ]
        out = self.compute(rows)
        self.assertEqual(out[0]["engagement"], "Current")
        self.assertEqual(out[1]["engagement"], "Lapsed")

    def test_tier_and_engagement_are_independent(self) -> None:
        """The original skill made "Lapsed" a tier, so a lapsed major donor
        matched two definitions with no precedence rule."""
        rows = [["D1", "Major", "Lapsed", 145000, 50000, 2020, "No", ""]]
        row = self.compute(rows)[0]
        self.assertEqual(row["tier"], "Platinum")
        self.assertEqual(row["engagement"], "Lapsed")
        self.assertEqual(row["segment"], "Lapsed")

    def test_lapsed_major_donor_is_routed_to_staff(self) -> None:
        rows = [["D1", "Major", "Lapsed", 145000, 50000, 2020, "No", ""]]
        row = self.compute(rows)[0]
        self.assertIn("lapsed_major_donor_personal_outreach_recommended", row["flags"])
        self.assertEqual(row["status"], "NEEDS_REVIEW")

    def test_loyalty_uplift_applies_only_to_the_prior_year(self) -> None:
        rows = [
            ["D1", "Gave", "LastYear", 60000, 10000, 2025, "No", ""],
            ["D2", "Gave", "Earlier", 60000, 10000, 2024, "No", ""],
        ]
        out = self.compute(rows)
        self.assertEqual(out[0]["ask_amount"], "4400")  # 4000 x 1.10
        self.assertEqual(out[1]["ask_amount"], "4000")

    def test_emergency_multiplier_applies(self) -> None:
        rows = [["D1", "Em", "Ergency", 60000, 10000, 2023, "No", ""]]
        self.assertEqual(self.compute(rows)[0]["ask_amount"], "4000")
        self.assertEqual(self.compute(rows, "--campaign", "emergency")[0]["ask_amount"], "4800")

    def test_tier_mismatch_blocks_the_letter(self) -> None:
        """Tier drives tone, salutation and offer, so a wrong tier is a wrong
        letter rather than a cosmetic data note."""
        rows = [["D1", "Says", "Silver", 17000, 5000, 2023, "No", "Silver"]]
        row = self.compute(rows)[0]
        self.assertIn("tier_mismatch(file=Silver,computed=Gold)", row["flags"])
        self.assertEqual(row["status"], "NEEDS_REVIEW")

    def test_bronze_flat_ask_far_above_largest_gift_is_held(self) -> None:
        rows = [["D1", "Tiny", "Donor", 50, 50, 2025, "No", ""]]
        row = self.compute(rows)[0]
        self.assertIn("flat_ask_exceeds_review_multiple_of_largest_gift", row["flags"])
        self.assertEqual(row["status"], "NEEDS_REVIEW")


class TestReviewGate(SkillTestCase):
    def test_capped_ask_is_held_for_review(self) -> None:
        """Regression: the cap flag used to be raised while the row still
        passed as OK, so SKILL.md promised a review that never happened."""
        rows = [["D1", "Cap", "Engages", 1000, 60, 2025, "Yes", ""]]
        row = self.compute(rows)[0]
        self.assertIn("ask_capped_at_largest_gift", row["flags"])
        self.assertEqual(row["status"], "NEEDS_REVIEW")

    def test_negative_amounts_are_held_not_computed(self) -> None:
        """Regression: a negative largest_gift (a refund artefact in a CRM
        export) used to yield a confident $50 ask with status OK."""
        rows = [
            ["D1", "Neg", "Largest", 60000, -100, 2025, "No", ""],
            ["D2", "Neg", "Lifetime", -500, 100, 2025, "No", ""],
        ]
        out = self.compute(rows)
        self.assertIn("negative_largest_gift", out[0]["flags"])
        self.assertEqual(out[0]["ask_amount"], "")
        self.assertEqual(out[0]["status"], "NEEDS_REVIEW")
        self.assertIn("negative_lifetime_total", out[1]["flags"])
        self.assertEqual(out[1]["status"], "NEEDS_REVIEW")

    def test_missing_financial_fields_are_never_guessed(self) -> None:
        rows = [
            ["D1", "No", "Largest", 20000, "", 2025, "No", ""],
            ["D2", "No", "Lifetime", "", 1500, 2025, "No", ""],
        ]
        out = self.compute(rows)
        for row in out:
            self.assertEqual(row["ask_amount"], "")
            self.assertEqual(row["status"], "NEEDS_REVIEW")

    def test_duplicate_names_and_ids_both_flagged(self) -> None:
        """A donor must not receive two letters, and two donors sharing an id
        would overwrite each other's letter file."""
        rows = [
            ["D1", "Same", "Person", 5000, 1500, 2025, "No", ""],
            ["D2", "Same", "Person", 5000, 1500, 2025, "No", ""],
            ["D9", "Shared", "Id", 5000, 1500, 2025, "No", ""],
            ["D9", "Other", "Donor", 5000, 1500, 2025, "No", ""],
        ]
        out = self.compute(rows)
        self.assertTrue(all("duplicate_donor_name" in r["flags"] for r in out[:2]))
        self.assertTrue(all("duplicate_donor_id" in r["flags"] for r in out[2:]))
        self.assertTrue(all(r["status"] == "NEEDS_REVIEW" for r in out))

    def test_review_threshold_flags_large_asks(self) -> None:
        rows = [["D1", "Big", "Ask", 500000, 200000, 2023, "No", ""]]
        self.assertEqual(self.compute(rows)[0]["status"], "OK")
        held = self.compute(rows, "--review-threshold", "10000")[0]
        self.assertIn("large_ask_review", held["flags"])
        self.assertEqual(held["status"], "NEEDS_REVIEW")

    def test_unknown_flags_block_by_default(self) -> None:
        """The status rule is a whitelist of harmless flags, so any flag added
        later holds the row instead of silently shipping."""
        self.assertEqual(compute_asks.NON_BLOCKING_FLAGS, {"no_title_on_file_used_full_name"})


class TestSuppression(SkillTestCase):
    def test_deceased_and_do_not_contact_are_suppressed(self) -> None:
        header = DONOR_HEADER + ["deceased", "do_not_contact"]
        src = write_csv(
            self.tmp / "donors.csv",
            header,
            [
                ["D1", "Est", "Ate", 5000, 1500, 2025, "No", "", "yes", ""],
                ["D2", "Do", "Not", 5000, 1500, 2025, "No", "", "", "yes"],
            ],
        )
        out = self.tmp / "review.csv"
        self.assertEqual(run(COMPUTE, str(src), str(out), "--campaign-year", "2026").returncode, 0)
        rows = read_rows(out)
        self.assertTrue(all(r["status"] == "SUPPRESSED" for r in rows))
        # No ask and no salutation, so a suppressed row cannot be mistaken
        # for a mailable one further down the pipeline.
        self.assertTrue(all(r["ask_amount"] == "" and r["salutation"] == "" for r in rows))

    def test_permission_suppression_follows_the_channel(self) -> None:
        """Regression: email permission used to suppress mail campaigns too,
        silently dropping valid recipients from a print mailing."""
        header = DONOR_HEADER + ["email_permission", "mail_permission"]
        src = write_csv(
            self.tmp / "donors.csv",
            header,
            [
                ["D1", "No", "Email", 5000, 1500, 2025, "No", "", "no", "yes"],
                ["D2", "No", "Mail", 5000, 1500, 2025, "No", "", "yes", "no"],
            ],
        )
        for channel, expected in (("email", ["SUPPRESSED", "OK"]), ("mail", ["OK", "SUPPRESSED"])):
            out = self.tmp / f"review_{channel}.csv"
            run(COMPUTE, str(src), str(out), "--campaign-year", "2026", "--channel", channel)
            self.assertEqual([r["status"] for r in read_rows(out)], expected, channel)


class TestSalutation(SkillTestCase):
    def test_no_title_falls_back_to_full_name_never_a_guess(self) -> None:
        """The original guessed honorifics from first names ("Elizabeth is
        probably Ms."), which misgenders donors."""
        rows = [["D1", "Elizabeth", "Whitfield", 60000, 10000, 2023, "No", ""]]
        row = self.compute(rows)[0]
        self.assertEqual(row["salutation"], "Dear Elizabeth Whitfield,")
        self.assertIn("no_title_on_file_used_full_name", row["flags"])
        # Informational only - this must not block the letter.
        self.assertEqual(row["status"], "OK")

    def test_supplied_title_is_used(self) -> None:
        header = DONOR_HEADER + ["title"]
        src = write_csv(
            self.tmp / "donors.csv",
            header,
            [["D1", "Elizabeth", "Whitfield", 60000, 10000, 2023, "No", "", "Dr."]],
        )
        out = self.tmp / "review.csv"
        run(COMPUTE, str(src), str(out), "--campaign-year", "2026")
        self.assertEqual(read_rows(out)[0]["salutation"], "Dear Dr. Whitfield,")


# ---------------------------------------------------------------------------
# compute_asks.py: input handling
# ---------------------------------------------------------------------------


class TestInputHandling(SkillTestCase):
    def _expect_error(self, *args: str) -> str:
        result = run(COMPUTE, *args)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result.stderr

    def test_empty_file_is_refused(self) -> None:
        path = self.tmp / "empty.csv"
        path.write_text("", encoding="utf-8")
        self.assertIn("no header row", self._expect_error(str(path), str(self.tmp / "o.csv")))

    def test_header_only_file_is_refused(self) -> None:
        path = write_csv(self.tmp / "hdr.csv", DONOR_HEADER, [])
        self.assertIn("no donor rows", self._expect_error(str(path), str(self.tmp / "o.csv")))

    def test_missing_required_columns_are_named(self) -> None:
        path = write_csv(self.tmp / "bad.csv", ["name", "amount"], [["Bob", 100]])
        message = self._expect_error(str(path), str(self.tmp / "o.csv"))
        self.assertIn("missing required columns", message)
        self.assertIn("first_name", message)

    def test_spreadsheet_gets_conversion_instructions(self) -> None:
        """Regression: an .xlsx used to produce a raw UnicodeDecodeError."""
        path = self.tmp / "donors.xlsx"
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
        message = self._expect_error(str(path), str(self.tmp / "o.csv"))
        self.assertIn("not a CSV", message)
        self.assertIn("CSV UTF-8", message)

    def test_semicolon_delimited_export_is_detected(self) -> None:
        """Excel on a European locale exports semicolons by default."""
        path = self.tmp / "semi.csv"
        path.write_text(
            "first_name;last_name;lifetime_total;largest_gift;last_gift_year\n"
            "Bo;Nilsson;5000;1500;2025\n",
            encoding="utf-8",
        )
        out = self.tmp / "review.csv"
        result = run(COMPUTE, str(path), str(out), "--campaign-year", "2026")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_rows(out)[0]["last_name"], "Nilsson")

    def test_bom_and_non_ascii_names_survive(self) -> None:
        path = self.tmp / "bom.csv"
        path.write_text(
            "first_name,last_name,lifetime_total,largest_gift,last_gift_year\n"
            "José,García-Öztürk,5000,1500,2025\n",
            encoding="utf-8-sig",
        )
        out = self.tmp / "review.csv"
        self.assertEqual(run(COMPUTE, str(path), str(out), "--campaign-year", "2026").returncode, 0)
        self.assertEqual(read_rows(out)[0]["last_name"], "García-Öztürk")

    def test_reprocessing_an_output_file_is_refused(self) -> None:
        """Re-running on an output clears tier_mismatch flags, because the
        output's tier column already holds computed values."""
        message = self._expect_error(str(GOLDEN), str(self.tmp / "o.csv"))
        self.assertIn("prior output of this script", message)

    def test_reprocessing_override_exists_for_deliberate_use(self) -> None:
        result = run(
            COMPUTE, str(GOLDEN), str(self.tmp / "o.csv"),
            "--campaign-year", "2026", "--allow-reprocessed",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# ---------------------------------------------------------------------------
# render_letters.py
# ---------------------------------------------------------------------------


class TestRendering(SkillTestCase):
    """End-to-end rendering from the golden review file."""

    RENDER_ARGS = (
        "--charity", "ASPCA",
        "--donation-url", "https://www.aspca.org/donate",
        "--signer-name", "Jane Okafor",
        "--signer-title", "Director of Development",
        "--date", "2026-08-02",
    )

    def render(self, review: Path, out_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return run(
            RENDER, str(review), str(out_dir),
            "--content", str(CONTENT_EXAMPLE), *self.RENDER_ARGS, *extra,
        )

    def test_renders_one_file_per_mailable_donor(self) -> None:
        out_dir = self.tmp / "letters"
        result = self.render(GOLDEN, out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        ok_ids = [r["donor_id"] for r in read_rows(GOLDEN) if r["status"] == "OK"]
        letters = sorted(p.name for p in out_dir.glob("letter_*.html"))
        self.assertEqual(len(letters), len(ok_ids))
        self.assertTrue((out_dir / "letters_manifest.csv").exists())

    def test_flagged_and_suppressed_rows_are_not_rendered(self) -> None:
        out_dir = self.tmp / "letters"
        self.render(GOLDEN, out_dir)
        rendered = {p.stem.removeprefix("letter_") for p in out_dir.glob("letter_*.html")}
        for row in read_rows(GOLDEN):
            if row["status"] != "OK":
                self.assertNotIn(row["donor_id"], rendered, row["donor_id"])

    def test_suppressed_rows_are_never_rendered_even_when_flagged_included(self) -> None:
        """There is deliberately no flag that mails a deceased or
        do-not-contact donor."""
        review = self.tmp / "review.csv"
        header = DONOR_HEADER + ["deceased"]
        src = write_csv(
            self.tmp / "donors.csv",
            header,
            [
                ["D1", "Est", "Ate", 5000, 1500, 2025, "No", "", "yes"],
                ["D2", "Fine", "Donor", 5000, 1500, 2025, "No", "", ""],
            ],
        )
        run(COMPUTE, str(src), str(review), "--campaign-year", "2026")
        out_dir = self.tmp / "letters"
        result = self.render(review, out_dir, "--include-flagged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((out_dir / "letter_D1.html").exists())
        self.assertTrue((out_dir / "letter_D2.html").exists())

    def test_donor_values_are_html_escaped(self) -> None:
        """A CSV cell is untrusted input. Escaping is enforced in code rather
        than left as an instruction the model might skip."""
        review = self.tmp / "review.csv"
        src = write_csv(
            self.tmp / "donors.csv",
            DONOR_HEADER,
            [["D1", "<script>alert(1)</script>", "O'Brien & Sons", 5000, 1500, 2025, "No", ""]],
        )
        run(COMPUTE, str(src), str(review), "--campaign-year", "2026")
        out_dir = self.tmp / "letters"
        self.assertEqual(self.render(review, out_dir).returncode, 0)
        letter = (out_dir / "letter_D1.html").read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", letter)
        self.assertNotIn("<script>", letter)

    def test_no_placeholders_or_template_comment_survive(self) -> None:
        out_dir = self.tmp / "letters"
        self.render(GOLDEN, out_dir)
        for path in out_dir.glob("letter_*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\[[A-Z][A-Z0-9_]{2,}\]", path.name)
            # The template's documentation comment must not reach donors.
            self.assertNotIn("<!--", text)

    def test_manifest_and_filenames_carry_no_donor_names(self) -> None:
        """Filenames travel further than file contents."""
        out_dir = self.tmp / "letters"
        self.render(GOLDEN, out_dir)
        names = {r["first_name"] for r in read_rows(GOLDEN) if r["first_name"]}
        manifest_text = (out_dir / "letters_manifest.csv").read_text(encoding="utf-8")
        filenames = " ".join(p.name for p in out_dir.glob("letter_*.html"))
        for name in names:
            self.assertNotIn(name, filenames)
            self.assertNotIn(name, manifest_text)

    def test_missing_content_block_is_refused(self) -> None:
        content = self.tmp / "content.json"
        content.write_text(
            json.dumps({"campaign_paragraphs": {"Gold": "x"}, "tier_lines": {"Gold": ""}}),
            encoding="utf-8",
        )
        result = run(
            RENDER, str(GOLDEN), str(self.tmp / "letters"),
            "--content", str(content), *self.RENDER_ARGS,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing text for", result.stderr)

    def test_content_scaffold_lists_the_segments_present(self) -> None:
        scaffold = self.tmp / "content.json"
        result = run(RENDER, str(GOLDEN), str(self.tmp / "letters"), "--emit-content", str(scaffold))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(scaffold.read_text(encoding="utf-8"))
        expected = {r["segment"] for r in read_rows(GOLDEN) if r["status"] == "OK"}
        self.assertEqual(set(data["campaign_paragraphs"]), expected)
        self.assertEqual(set(data["tier_lines"]), expected)

    def test_required_campaign_inputs_are_enforced(self) -> None:
        """Notably the signer: the original skill invented a relationship
        manager name, which cannot survive a donor's reply."""
        result = run(
            RENDER, str(GOLDEN), str(self.tmp / "letters"),
            "--content", str(CONTENT_EXAMPLE), "--charity", "ASPCA",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("--signer-name", result.stderr)

    def test_bad_donation_url_is_refused(self) -> None:
        result = run(
            RENDER, str(GOLDEN), str(self.tmp / "letters"),
            "--content", str(CONTENT_EXAMPLE),
            "--charity", "ASPCA", "--donation-url", "aspca.org/donate",
            "--signer-name", "J", "--signer-title", "D",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("http", result.stderr)

    def test_will_not_mix_two_campaigns_in_one_directory(self) -> None:
        out_dir = self.tmp / "letters"
        self.assertEqual(self.render(GOLDEN, out_dir).returncode, 0)
        second = self.render(GOLDEN, out_dir)
        self.assertEqual(second.returncode, 1)
        self.assertIn("already contains", second.stderr)
        self.assertEqual(self.render(GOLDEN, out_dir, "--force").returncode, 0)

    def test_wrong_input_file_is_diagnosed(self) -> None:
        result = self.render(FIXTURE, self.tmp / "letters")
        self.assertEqual(result.returncode, 1)
        self.assertIn("review CSV written by compute_asks.py", result.stderr)

    def test_limit_renders_a_preview_subset(self) -> None:
        out_dir = self.tmp / "letters"
        result = self.render(GOLDEN, out_dir, "--limit", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(out_dir.glob("letter_*.html"))), 2)

    def test_donor_id_cannot_escape_the_output_directory(self) -> None:
        """A donor_id is attacker-influenced data that becomes a filename."""
        review = self.tmp / "review.csv"
        src = write_csv(
            self.tmp / "donors.csv",
            DONOR_HEADER,
            [["../../escape", "Path", "Traversal", 5000, 1500, 2025, "No", ""]],
        )
        run(COMPUTE, str(src), str(review), "--campaign-year", "2026")
        out_dir = self.tmp / "letters"
        self.assertEqual(self.render(review, out_dir).returncode, 0)
        written = list(out_dir.glob("letter_*.html"))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].parent.resolve(), out_dir.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
