#!/usr/bin/env python3
"""
render_letters.py - merge the review CSV into per-donor HTML letters.

WHY THIS EXISTS
---------------
compute_asks.py made the *numbers* deterministic, but if the assistant then
hand-writes one HTML file per donor, three problems come back:

  1. It does not scale. A 40,000-row mailing means 40,000 model-written
     files. The maths for that list takes under a second; the writing does
     not finish at all.
  2. It is not reproducible. The same donor gets different prose on
     different runs, which is exactly what the brief asked to avoid.
  3. HTML-escaping becomes an instruction the model may forget. A donor
     named `O'Brien & Sons` breaks the markup; a CSV cell containing
     `<script>` is worse.

So the division of labour is: the model writes a small number of prose
blocks - one campaign paragraph and one tier line per segment, ~10 short
pieces of writing for a whole campaign - and this script stamps them into
every letter with escaping enforced in code.

USAGE
-----
    # 1. Emit a content scaffold covering the segments actually present:
    python render_letters.py asks_review.csv letters/ --emit-content content.json

    # 2. Fill in content.json, then render:
    python render_letters.py asks_review.csv letters/ \
        --content content.json \
        --charity "ASPCA" \
        --donation-url "https://www.aspca.org/donate" \
        --signer-name "Jane Okafor" \
        --signer-title "Director of Development" \
        [--template ../assets/letter_template.html] \
        [--date 2026-08-02] \
        [--limit 3] \
        [--force]

Exit codes: 0 = success, 1 = fatal input/config error, 2 = bad CLI usage.

SAFETY PROPERTIES
-----------------
  * SUPPRESSED rows are never rendered. There is no flag to override this.
  * NEEDS_REVIEW rows are not rendered without --include-flagged, which
    exists for the case where staff have reviewed and accepted the rows.
  * --force clears the previous run's letters and manifest rather than
    writing over some of them, so a shorter second run cannot leave another
    campaign's letters behind. Every letter is preflight-rendered first, so
    an input, template or content failure cannot destroy good output.
  * Filenames are resolved for the whole batch before anything is written,
    and compared case-insensitively, so no donor can silently overwrite
    another on a case-insensitive filesystem.
  * The closing call to action follows the delivery channel recorded in the
    review CSV: a printed letter does not tell the donor to hit reply.
  * Every value that came from the donor file is HTML-escaped.
  * Content blocks from content.json are treated as trusted HTML fragments
    (they are written by staff/the assistant, not by the data) so that
    <em>, <a> and friends work - keep donor data out of them.
  * A leftover [PLACEHOLDER] in any rendered letter is a fatal error, not a
    letter mailed with a literal bracket in it.
"""

import argparse
import csv
import html
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, NoReturn, cast

# Placeholders the template may contain. Anything matching PLACEHOLDER_RE
# that is still present after substitution stops the run.
PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9_]{2,}\]")

# Columns render_letters needs from the review CSV produced by compute_asks.py.
REQUIRED_REVIEW_COLUMNS = {
    "donor_id",
    "segment",
    "salutation",
    "lifetime_total_display",
    "ask_amount_display",
    "status",
}

# Every segment compute_asks.py can emit. Used to build the content scaffold.
KNOWN_SEGMENTS = ["Platinum", "Gold", "Silver", "Bronze", "Lapsed"]

# The closing call to action, per delivery channel. "Reply to this email"
# is wrong on a printed letter, so the wording follows the channel the
# review file was computed for rather than being fixed in the template.
# {url} is substituted with the already-escaped donation URL.
CALL_TO_ACTION = {
    "email": "To give, simply reply to this email or visit <strong>{url}</strong>.",
    "mail": "To give, visit <strong>{url}</strong>.",
}
DEFAULT_CHANNEL = "email"

# donor_id goes straight into a filename, so restrict it to characters that
# are safe on every filesystem. Prevents a CSV cell like "../../etc/passwd"
# or "CON" from steering where files land.
UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def fail(message: str) -> NoReturn:
    """Exit(1) with the standard error prefix used across this skill."""
    sys.exit(f"ERROR: {message}")


def positive_int(raw: str) -> int:
    """argparse type for counts that must be 1 or more.

    Without this, --limit 0 is falsy and renders the whole list, and a
    negative value slices from the end - both silently produce a mailing
    nobody asked for.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or greater (got {value})")
    return value


def review_channel(rows: list[dict[str, str]]) -> str:
    """Determine which delivery channel the review file was computed for.

    Older review files predate the column, so its absence falls back to
    email rather than failing. A file containing more than one channel was
    assembled by hand from separate runs and is refused: the suppression
    rules applied to those rows disagree with each other.
    """
    channels = {(row.get("channel") or "").strip().lower() for row in rows}
    channels.discard("")
    if not channels:
        return DEFAULT_CHANNEL
    if len(channels) > 1:
        fail(
            f"review CSV mixes delivery channels {sorted(channels)}. Each run of "
            f"compute_asks.py applies one channel's suppression rules; render "
            f"each run's output separately."
        )
    channel = channels.pop()
    if channel not in CALL_TO_ACTION:
        fail(
            f"review CSV has unknown channel {channel!r} "
            f"(expected one of {sorted(CALL_TO_ACTION)})"
        )
    return channel


def safe_filename_part(donor_id: str) -> str:
    """Reduce a donor_id to filesystem-safe characters."""
    cleaned = UNSAFE_FILENAME_CHARS.sub("_", donor_id).strip("._") or "unknown"
    return cleaned[:64]  # keep well under path-length limits


def strip_leading_comments(template: str) -> str:
    """Remove HTML comments that precede the opening <html> tag.

    The bundled template documents its own placeholders in a header comment.
    That comment must not reach donors, and it mentions placeholder names
    literally - which would otherwise trip the leftover-placeholder check on
    every letter. Comments inside the body are left alone, because
    conditional comments (<!--[if mso]>) are a legitimate email technique.
    """
    head_end = template.lower().find("<html")
    if head_end == -1:
        return template
    head, body = template[:head_end], template[head_end:]
    return re.sub(r"<!--.*?-->", "", head, flags=re.DOTALL).lstrip() + body


# --------------------------------------------------------------------------
# Content blocks
# --------------------------------------------------------------------------


def content_scaffold(segments: list[str]) -> dict[str, dict[str, str]]:
    """Build an empty content.json for the given segments.

    Ordered by KNOWN_SEGMENTS so the file reads top-tier-down regardless of
    what order the segments appeared in the data.
    """
    ordered = [s for s in KNOWN_SEGMENTS if s in segments] + [
        s for s in sorted(segments) if s not in KNOWN_SEGMENTS
    ]
    return {
        "campaign_paragraphs": {s: "" for s in ordered},
        "tier_lines": {s: "" for s in ordered},
    }


def load_content(path: str, needed_segments: set[str]) -> dict[str, dict[str, str]]:
    """Load and validate the prose blocks.

    Fails loudly on a missing or blank block rather than rendering a letter
    with an empty paragraph where the appeal should be.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        fail(
            f"content file not found: {path}. Generate a scaffold first with "
            f"--emit-content {path}"
        )
    except json.JSONDecodeError as exc:
        fail(f"content file {path} is not valid JSON: {exc}")
    except OSError as exc:
        fail(f"could not read content file {path}: {exc}")

    if not isinstance(raw, dict):
        fail("content JSON must be an object with 'campaign_paragraphs' and 'tier_lines'")
    content = cast(dict[str, Any], raw)

    missing_sections = {"campaign_paragraphs", "tier_lines"} - set(content)
    if missing_sections:
        fail(f"content file is missing section(s): {sorted(missing_sections)}")

    problems: list[str] = []
    for section in ("campaign_paragraphs", "tier_lines"):
        block = content[section]
        if not isinstance(block, dict):
            fail(f"content section '{section}' must be an object keyed by segment")
        block_map = cast(dict[str, Any], block)
        for segment in sorted(needed_segments):
            value = block_map.get(segment)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{section}.{segment}")

    if problems:
        fail(
            "content file is missing text for: "
            + ", ".join(problems)
            + ". Every segment present in the mailable rows needs a campaign "
            "paragraph and a tier line."
        )

    return cast(dict[str, dict[str, str]], content)


# --------------------------------------------------------------------------
# Review CSV
# --------------------------------------------------------------------------


def load_review_rows(path: str) -> list[dict[str, str]]:
    """Read the review CSV emitted by compute_asks.py and check its shape."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                fail(f"{path} has no header row")
            missing = REQUIRED_REVIEW_COLUMNS - {n.strip() for n in reader.fieldnames}
            if missing:
                fail(
                    f"{path} is missing columns {sorted(missing)}. This script "
                    f"expects the review CSV written by compute_asks.py, not the "
                    f"raw donor export."
                )
            rows = list(reader)
    except FileNotFoundError:
        fail(f"review CSV not found: {path}")
    except UnicodeDecodeError:
        fail(f"{path} is not valid UTF-8 text")
    except OSError as exc:
        fail(f"could not read {path}: {exc}")

    if not rows:
        fail(f"{path} contains no rows")
    return rows


def select_rows(rows: list[dict[str, str]], include_flagged: bool) -> list[dict[str, str]]:
    """Return the rows that may be rendered, refusing suppressed donors outright."""
    selected: list[dict[str, str]] = []
    for row in rows:
        status = (row.get("status") or "").strip().upper()
        if status == "SUPPRESSED":
            # Deliberately unconditional: there is no CLI flag that renders
            # a letter to a deceased or do-not-contact donor.
            continue
        if status == "OK" or (include_flagged and status == "NEEDS_REVIEW"):
            selected.append(row)
    return selected


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_one(
    template: str,
    row: dict[str, str],
    content: dict[str, dict[str, str]],
    campaign_fields: dict[str, str],
) -> str:
    """Fill the template for a single donor.

    Donor-derived values are escaped; campaign fields (typed by staff on the
    command line) are escaped too, since they are plain text in the letter.
    Content blocks are inserted as-is so staff can use inline markup.
    """
    segment = (row.get("segment") or "").strip()
    values = {
        # --- from the donor file, therefore untrusted: escape ---
        "SALUTATION": html.escape(row.get("salutation", "")),
        "LIFETIME_TOTAL": html.escape(row.get("lifetime_total_display", "")),
        "ASK_AMOUNT": html.escape(row.get("ask_amount_display", "")),
        # --- from the operator ---
        "DATE": html.escape(campaign_fields["date"]),
        "CHARITY_NAME": html.escape(campaign_fields["charity"]),
        "SIGNER_NAME": html.escape(campaign_fields["signer_name"]),
        "SIGNER_TITLE": html.escape(campaign_fields["signer_title"]),
        # Built from the channel; contains the escaped URL already.
        "CALL_TO_ACTION": campaign_fields["call_to_action"],
        # --- authored prose, trusted as HTML ---
        "CAMPAIGN_PARAGRAPH": content["campaign_paragraphs"][segment],
        "TIER_SPECIFIC_LINE": content["tier_lines"][segment],
    }

    letter = template
    for key, value in values.items():
        letter = letter.replace(f"[{key}]", value)

    leftover = PLACEHOLDER_RE.findall(letter)
    if leftover:
        fail(
            f"template still contains {sorted(set(leftover))} after rendering "
            f"donor {row.get('donor_id', '?')}. Either the template uses a "
            f"placeholder this script does not supply, or a value was blank."
        )
    return letter


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Render per-donor HTML letters from a compute_asks.py review CSV."
    )
    ap.add_argument("review_csv", help="Review CSV written by compute_asks.py")
    ap.add_argument("output_dir", help="Directory to write letter files into")
    ap.add_argument(
        "--emit-content",
        metavar="PATH",
        default=None,
        help="Write an empty content scaffold for the segments present, then exit",
    )
    ap.add_argument("--content", default=None, help="JSON file of campaign paragraphs and tier lines")
    ap.add_argument("--charity", default=None, help="Charity name as it should appear in letters")
    ap.add_argument("--donation-url", default=None, help="Donation page URL")
    ap.add_argument("--signer-name", default=None, help="Real name of the staff member signing")
    ap.add_argument("--signer-title", default=None, help="Job title of the signer")
    ap.add_argument(
        "--template",
        default=str(Path(__file__).resolve().parent.parent / "assets" / "letter_template.html"),
        help="HTML template (defaults to the bundled assets/letter_template.html)",
    )
    ap.add_argument("--date", default=None, help="Letter date, YYYY-MM-DD (defaults to today)")
    ap.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help="Render at most N letters, N >= 1 (for previews)",
    )
    ap.add_argument(
        "--include-flagged",
        action="store_true",
        help="Also render NEEDS_REVIEW rows (only after staff have reviewed them). "
        "SUPPRESSED rows are never rendered.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite letters already present in the output directory",
    )
    return ap


def main() -> None:
    args = build_arg_parser().parse_args()

    rows = load_review_rows(args.review_csv)
    selected = select_rows(rows, args.include_flagged)

    suppressed_count = sum(
        1 for r in rows if (r.get("status") or "").strip().upper() == "SUPPRESSED"
    )
    flagged_count = sum(
        1 for r in rows if (r.get("status") or "").strip().upper() == "NEEDS_REVIEW"
    )

    if not selected:
        fail(
            "no mailable rows. "
            f"{flagged_count} row(s) are NEEDS_REVIEW and {suppressed_count} are "
            f"SUPPRESSED. Resolve the flagged rows in the review CSV first."
        )

    # A row can be selected and still be unrenderable - this happens with
    # --include-flagged, where a reviewed row may still be missing the very
    # fields the letter needs. Refuse with the specific rows named, rather
    # than rendering "a gift of $." or a letter with no tone.
    unrenderable: list[str] = []
    for row in selected:
        donor_id = (row.get("donor_id") or "?").strip()
        if not (row.get("segment") or "").strip():
            unrenderable.append(f"{donor_id} (no segment - tier could not be computed)")
        elif not (row.get("ask_amount_display") or "").strip():
            unrenderable.append(f"{donor_id} (no ask amount)")
    if unrenderable:
        shown = ", ".join(unrenderable[:10])
        more = f" and {len(unrenderable) - 10} more" if len(unrenderable) > 10 else ""
        fail(
            f"{len(unrenderable)} selected row(s) cannot be rendered: {shown}{more}. "
            f"Correct them in the source export and re-run compute_asks.py."
        )

    segments_present = {(r.get("segment") or "").strip() for r in selected}

    # --emit-content is a scaffolding mode: write the skeleton and stop, so
    # the assistant knows exactly which blocks it has to write.
    if args.emit_content:
        scaffold = content_scaffold(sorted(segments_present))
        try:
            with open(args.emit_content, "w", encoding="utf-8") as f:
                json.dump(scaffold, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as exc:
            fail(f"could not write {args.emit_content}: {exc}")
        print(f"Wrote content scaffold to {args.emit_content}")
        print(f"  Segments needing text: {', '.join(sorted(segments_present))}")
        print(f"  Blocks to write: {len(segments_present) * 2}")
        print("Fill in every value, then re-run with --content to render.")
        return

    # Everything below actually renders, so all campaign inputs are required.
    required_args = {
        "--content": args.content,
        "--charity": args.charity,
        "--donation-url": args.donation_url,
        "--signer-name": args.signer_name,
        "--signer-title": args.signer_title,
    }
    missing_args = [name for name, value in required_args.items() if not value]
    if missing_args:
        fail(
            f"missing required option(s): {', '.join(missing_args)}. "
            f"The signer must be a real member of staff - a letter signed by an "
            f"invented name cannot survive a donor's reply."
        )

    if not re.match(r"^https?://", args.donation_url):
        fail(f"--donation-url must start with http:// or https:// (got {args.donation_url!r})")

    if args.date:
        try:
            letter_date = date.fromisoformat(args.date).strftime("%B %d, %Y")
        except ValueError:
            fail(f"--date must be YYYY-MM-DD (got {args.date!r})")
    else:
        letter_date = date.today().strftime("%B %d, %Y")

    content = load_content(args.content, segments_present)

    try:
        template = strip_leading_comments(Path(args.template).read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"template not found: {args.template}")
    except OSError as exc:
        fail(f"could not read template {args.template}: {exc}")

    out_dir = Path(args.output_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"could not create output directory {out_dir}: {exc}")

    # Refuse to mix two campaigns' output in one folder by accident - the
    # stale letters look identical to the fresh ones once they are on disk.
    existing = list(out_dir.glob("letter_*.html"))
    if existing and not args.force:
        fail(
            f"{out_dir} already contains {len(existing)} letter file(s). "
            f"Use a fresh directory, or pass --force to replace them."
        )

    channel = review_channel(rows)
    campaign_fields = {
        "date": letter_date,
        "charity": args.charity,
        "signer_name": args.signer_name,
        "signer_title": args.signer_title,
        "call_to_action": CALL_TO_ACTION[channel].format(url=html.escape(args.donation_url)),
    }

    to_render = selected[: args.limit] if args.limit is not None else selected

    # Resolve every filename before writing anything, so a collision is
    # reported instead of one donor silently overwriting another. Compared
    # case-insensitively because Windows and macOS filesystems are: "D1" and
    # "d1" are distinct IDs that resolve to the same file.
    filenames: list[str] = []
    claimed: dict[str, str] = {}
    for row in to_render:
        donor_id = (row.get("donor_id") or "").strip()
        if not donor_id:
            fail("a mailable row has no donor_id; re-run compute_asks.py")
        filename = f"letter_{safe_filename_part(donor_id)}.html"
        previous = claimed.get(filename.casefold())
        if previous is not None:
            fail(
                f"donor_id {donor_id!r} and {previous!r} both resolve to "
                f"{filename}, so one letter would overwrite the other. Give "
                f"donors IDs that differ by more than case or punctuation."
            )
        claimed[filename.casefold()] = donor_id
        filenames.append(filename)

    # Preflight every deterministic render before removing the previous
    # campaign. This catches unsupported/leftover template placeholders and
    # row-specific missing values without retaining every rendered letter in
    # memory. render_one has no side effects, so it is safe to call again in
    # the write loop below.
    for row in to_render:
        render_one(template, row, content, campaign_fields)

    # Clear the previous campaign only now that every input and every letter
    # has validated. Filesystem write failures are still reported normally,
    # but no known template/content error can destroy the last good output.
    if existing and args.force:
        stale = [*existing, out_dir / "letters_manifest.csv"]
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                fail(f"could not remove stale file {path}: {exc}")

    manifest: list[dict[str, str]] = []
    for row, filename in zip(to_render, filenames):
        donor_id = (row.get("donor_id") or "").strip()
        letter = render_one(template, row, content, campaign_fields)
        try:
            (out_dir / filename).write_text(letter, encoding="utf-8")
        except OSError as exc:
            fail(f"could not write {out_dir / filename}: {exc}")

        manifest.append(
            {
                # Donor names are deliberately absent from the manifest and
                # from filenames: these files travel further than the letters.
                "donor_id": donor_id,
                "segment": (row.get("segment") or "").strip(),
                "status": (row.get("status") or "").strip(),
                "ask_amount": (row.get("ask_amount") or "").strip(),
                "file": filename,
            }
        )

    manifest_path = out_dir / "letters_manifest.csv"
    try:
        with open(manifest_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
            writer.writeheader()
            writer.writerows(manifest)
    except OSError as exc:
        fail(f"could not write {manifest_path}: {exc}")

    print(f"Rendered {len(manifest)} letter(s) to {out_dir} | channel={channel}")
    if existing and args.force:
        print(f"  --force replaced {len(existing)} letter(s) from a previous run")
    if args.limit and len(selected) > len(to_render):
        print(f"  --limit {args.limit} applied; {len(selected) - len(to_render)} mailable row(s) not rendered")
    if flagged_count:
        verb = "included" if args.include_flagged else "skipped"
        print(f"  {flagged_count} NEEDS_REVIEW row(s) {verb}")
    if suppressed_count:
        print(f"  {suppressed_count} SUPPRESSED row(s) skipped - these donors must not be contacted")
    print(f"  Manifest: {manifest_path}")
    print("These are drafts. Have a staff member review them before sending.")


if __name__ == "__main__":
    main()
