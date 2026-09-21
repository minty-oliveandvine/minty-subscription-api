"""Diff two ``replay_scenarios --report`` logs - Flask's and Django's - after normalising what
legitimately differs between two runs of the same shape on the same day.

    python scripts/replay_diff.py <flask run>/X1.log.txt <django run>/X1.log.txt --tags X1=DX1

The logs are not kept (user's decision): capture a run's stdout wherever is convenient, diff,
discard. ``docs/features/subscriptions-api.md`` §8 says how to regenerate a run.

What is normalised (and why it may differ without being a bug):

* Stripe ids (``pm_…``, ``in_…``, ``cus_…``, ``clock_…``) and uuids - every run mints its own.
* The entity-name tag: a Django run is cloned to a fresh payer under a new tag (``--as … --tag
  DX1``), so ``DX1 Only Co`` is Flask's ``X1 Only Co`` (``--tags FLASK=DJANGO``, repeatable).
* Timezone rendering of the anchor: psycopg2 hands Flask the session's zone (+08:00), Django
  renders UTC; both are parsed and compared as instants.
* Console encoding: em dashes and the card bullets come out as replacement characters when a
  log was captured through cp1252; punctuation is folded.
* Everything before the replay (setup, prune) and any per-day ``!! test clock`` lines (the
  ``_needs_stripe_clock`` bug both scripts had until 2026-09-21 - fixed, kept in the filter so
  the older logs still diff); the events and jobs of each day are compared, the noise is not.
  The payer-id prefix the dunning lines print (a clone is a new payer) and a log line that
  landed mid-line are dropped too.
* The order of invoices sharing an ``issued_at`` (see ``_canonical_invoice_order``).

NOT normalised, on purpose: the replay truncates entity names to fixed widths, so a clone tag
LONGER than the Flask run's loses trailing characters of every long name. Clone with a tag of
the SAME length (``Ang`` -> ``Dng``, not ``DAng``) rather than teaching this tool to guess.

Exit 0 when the two normalised transcripts (events, jobs, INVOICES, ACCOUNT, cards, STATE)
are identical; 1 with a unified diff otherwise. A difference is a bug in the port, or a Flask
bug the port found - never something to normalise away without saying so here.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from datetime import datetime
from pathlib import Path

ID_PATTERNS = [
    # a log line that landed mid-line (stderr interleaved with stdout) is not report content
    (re.compile(r"20\d\d-\d\d-\d\dT[\d:]+Z (ERROR|WARNING|INFO) billing-api .*$"), ""),
    (re.compile(r"\b(pm|in|cus|clock|sub|pi|ch|seti|si)_[A-Za-z0-9]+"), r"\1_XXX"),
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "UUID"),
    # the dunning lines print the payer id's first eight characters; the clone is a new payer
    (re.compile(r"(dunning (?:RECOVERED|gave up)) [0-9a-f]{8}"), r"\1 PAYER"),
    (re.compile(r"renewal keys scoped to \S+"), "renewal keys scoped to XXX"),
]
NOISE = re.compile(
    r"^\s*!! test clock: 'UserStripeCustomer' object has no attribute|"
    r"^\s*(created|reusing|attached|payer|prune|renewal keys|clock |  \S+_DATABASE_URI|  \d+ dangling)|"
    r"^(The |Scenario|Scenarios|Dunning|Two |Un-cancel|Anchored|Run )"
)
DAY = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4}$")
ANCHOR = re.compile(r"anchor=(\S+ \S+)")
PUNCT = str.maketrans({"—": "-", "–": "-", "•": "*", "�": "?"})


def _fold(text: str) -> str:
    # cp1252-captured logs turn every non-latin-1 glyph into U+FFFD or '?'; fold both sides
    text = text.translate(PUNCT)
    text = text.replace("????", "****").replace("? ", "- ").replace(" - as ", " - as ")
    return text


def normalise(path: Path, tag_map: dict[str, str]) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    out: list[str] = []
    started = False
    for line in raw.splitlines():
        if line.startswith("2026-") or line.startswith("20") and " | " in line:
            continue  # loguru / django log lines
        if line.startswith("replaying "):
            started = True
            line = re.sub(r"^replaying \S+:", "replaying RUN:", line)
        if not started:
            continue
        if NOISE.search(line) or not line.strip():
            continue
        line = _fold(line)
        for pat, rep in ID_PATTERNS:
            line = pat.sub(rep, line)
        for flask_tag, django_tag in tag_map.items():
            line = re.sub(rf"\b{re.escape(django_tag)}\b", flask_tag, line)
        m = ANCHOR.search(line)
        if m:
            try:
                instant = datetime.fromisoformat(m.group(1))
                line = line.replace(m.group(1), instant.astimezone().strftime("%Y-%m-%d %H:%M UTC")
                                    if instant.tzinfo is None else
                                    instant.astimezone(tz=__import__("datetime").UTC).strftime("%Y-%m-%d %H:%M UTC"))
            except ValueError:
                pass
        # Column padding shifts with the tag length; compare tokens, not columns.
        line = re.sub(r"[ \t]+", " ", line).rstrip()
        out.append(line)
    return _canonical_invoice_order(out)


INVOICE_HEADER = re.compile(r"^ \d{2} [A-Z][a-z]{2} \d{4} |^ not issued ")


def _canonical_invoice_order(lines: list[str]) -> list[str]:
    """Sort the INVOICES section's blocks (header + its lines) into a canonical order.

    Both reports order by ``issued_at`` with NULLS LAST and nothing else, so invoices that
    share an ``issued_at`` come out in whatever order the database found them. Two cards
    renewing on one day tie legitimately; under the (fixed) ``_needs_stripe_clock`` bug every
    renewal after the last event tied. Same set of documents in a different order is not a
    difference in what was billed.
    """
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("INVOICES:"))
        end = next(i for i, ln in enumerate(lines) if ln.startswith("ACCOUNT "))
    except StopIteration:
        return lines
    blocks: list[list[str]] = []
    for ln in lines[start + 1:end]:
        if INVOICE_HEADER.match(ln) or not blocks:
            blocks.append([ln])
        else:
            blocks[-1].append(ln)
    # Within an invoice the lines come out in whatever order the pending-extension rows
    # were read (an unordered query on both sides); the header stays first.
    blocks = [[b[0], *sorted(b[1:])] for b in blocks]
    blocks.sort(key=lambda b: "\n".join(b))
    return _canonical_day_order(lines[:start]) + [lines[start]] + [ln for b in blocks for ln in b] + lines[end:]


def _canonical_day_order(lines: list[str]) -> list[str]:
    """Within a day, sort each run of consecutive lines that share a first token.

    The jobs report in set / query order - ``sweep-access`` walks ``set(MODULE_CODES)``, whose
    iteration order is the process's hash seed - so two runs of the SAME code can print
    ``ACCESS revoked PETTY_CASH`` and ``ACCESS revoked PAYMENT_REQUEST`` either way round.
    The script's own events are already sorted; sorting a run of them again changes nothing.
    """
    out: list[str] = []
    run: list[str] = []
    key = None
    for ln in lines:
        token = ln.split(" ", 2)[1] if ln.startswith(" ") and len(ln.split(" ", 2)) > 1 else None
        if token is not None and token == key:
            run.append(ln)
            continue
        out.extend(sorted(run))
        run, key = ([ln], token) if token is not None else ([], None)
        if token is None:
            out.append(ln)
    out.extend(sorted(run))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("flask_log", type=Path)
    parser.add_argument("django_log", type=Path)
    parser.add_argument("--tags", action="append", default=[], metavar="FLASK=DJANGO",
                        help="entity-name tag of the Flask run and of the Django clone (repeatable)")
    args = parser.parse_args()
    tag_map = dict(item.split("=", 1) for item in args.tags)

    left = normalise(args.flask_log, {})
    right = normalise(args.django_log, tag_map)
    if left == right:
        print(f"IDENTICAL: {len(left)} normalised line(s)")
        return 0
    for line in difflib.unified_diff(left, right, str(args.flask_log), str(args.django_log), lineterm="", n=2):
        print(line)
    print(f"DIFFERENT: {sum(1 for d in difflib.ndiff(left, right) if d[:1] in '+-')} changed line(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
