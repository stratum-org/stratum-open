"""Findings as SARIF 2.1.0, the format GitHub's code scanning reads, as do
editors and other CI tools: `stratum-open review --sarif FILE` and the
Action's `sarif` input.

Each finding on a line is a result:
- its rule is the check's rule id. The model's findings go under
  `model/<category>`: a model finding's own rule is its title as a slug,
  whose wording changes from run to run, and code scanning tracks an alert
  by its rule;
- its level comes from the severity: critical and high are `error`, medium
  `warning`, low `note`. `properties.severity` keeps ours;
- `properties.source` is `check` or `model`, and the rule is tagged the
  same way, so a team can leave the model's findings out;
- the fingerprint is its `primaryLocationLineHash`, the one partial
  fingerprint code scanning reads. It keys on the code, not the line
  number, so an alert follows its code when lines move. Without it,
  github/codeql-action/upload-sarif hashes the lines of the files it finds
  on disk, which in a `pull_request_target` workflow are the default
  branch's, not the PR's.

Code scanning shows a result only on a line, so a finding without one
(about a whole file, or the PR as a whole) isn't in `results`. It's in the
run's `properties.findingsWithoutLine`, in the shape a result would have,
so the file keeps everything the review found and code scanning still
takes it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from stratum_open import __version__
from stratum_open.findings import Finding, rank

SCHEMA = "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
HOME = "https://github.com/stratum-org/stratum-open"
HELP = f"{HOME}#what-it-does-on-each-pr"
LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note"}

# What GitHub's code scanning takes in one upload, from
# https://docs.github.com/en/code-security/reference/code-scanning/sarif-files/sarif-support
# ("File compatibility", and the property tables after it):
# - 25,000 results a run, of which it keeps only the 5,000 most severe. The cut is made here
#   instead, the same way, so the file can say how many were left out.
MAX_RESULTS = 5_000
# - 10 MB for the gzipped file. The file itself stays under that, so the compression is a margin.
MAX_BYTES = 10_000_000
# - 1,024 characters for a rule's short and full descriptions.
MAX_DESCRIPTION = 1_024
# The rest need no cap here: one run a file (20 allowed), one location a result (1,000, and a
# result's own table says 10, of which code scanning reads the first), two tags a rule (20, of
# which 10 are shown), and a rule for each rule id found: the checks' ids and the model's eight
# categories, far under the 25,000 rules a run may have. Rules have no `name` (255 characters),
# so code scanning shows their id.


def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    """The SARIF log of these findings, cut to GitHub's limits with the most severe kept."""
    ranked = rank(findings)
    results = [f for f in ranked if f.anchored][:MAX_RESULTS]
    without = [f for f in ranked if not f.anchored]
    log = _log(results, without, len(findings))
    size = len(_text(log).encode("utf-8"))
    while size > MAX_BYTES and (results or without):
        cut = results or without  # the results go first, the least severe of them first
        del cut[min(len(cut) - 1, int(len(cut) * MAX_BYTES / size)):]  # by the share that's over: a pass or two
        log = _log(results, without, len(findings))
        size = len(_text(log).encode("utf-8"))
    return log


def write(path: str | Path, findings: list[Finding]) -> str:
    """Write the findings to `path` as SARIF, and return a line saying what's in it."""
    log = to_sarif(findings)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_text(log), encoding="utf-8", newline="\n")
    run = log["runs"][0]
    results, without, left_out = len(run["results"]), len(run["properties"]["findingsWithoutLine"]), run["properties"]["leftOut"]
    line = f"SARIF: {results} {'result' if results == 1 else 'results'} in {path}."
    if without:
        line += (f" {without} {'finding' if without == 1 else 'findings'} without a line {'is' if without == 1 else 'are'} in "
                 "runs[0].properties.findingsWithoutLine instead: code scanning shows a result only on a line.")
    if left_out:
        line += (f" {left_out} left out, the least severe, to stay within code scanning's limits "
                 f"({MAX_RESULTS:,} results, {MAX_BYTES // 1_000_000} MB).")
    return line


def rule_id(finding: Finding) -> str:
    """A check's rule id, or `model/<category>` for the model's (see the module's docstring)."""
    return f"model/{finding.category}" if finding.source == "model" else finding.rule


def _log(results: list[Finding], without: list[Finding], total: int) -> dict[str, Any]:
    rules: dict[str, dict[str, Any]] = {}
    for finding in results + without:
        rules.setdefault(rule_id(finding), _rule(finding))
    return {
        "$schema": SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "Stratum Open", "version": __version__, "informationUri": HOME, "rules": list(rules.values())}},
            "results": [_result(f) for f in results],
            "properties": {
                "findingsWithoutLine": [_result(f) for f in without],
                "leftOut": total - len(results) - len(without),
            },
        }],
    }


def _rule(finding: Finding) -> dict[str, Any]:
    """There's no table of rule descriptions to draw on, so a rule says who found it, and each
    result's message says what."""
    if finding.source == "model":
        short = f"Model review: {finding.category}"
        full = ("Found by the model's review of the PR, which can be wrong. These results have `source: model` in their "
                "properties and the `model` tag, so they can be left out. Each result's message says what was found.")
    else:
        short = f"Check: {finding.rule}"
        full = "Found by one of Stratum Open's checks, which need no model. Each result's message says what was found."
    return {
        "id": rule_id(finding),
        "shortDescription": {"text": short[:MAX_DESCRIPTION]},
        "fullDescription": {"text": full[:MAX_DESCRIPTION]},
        "help": {"text": f"{full} What Stratum Open looks for: {HELP}", "markdown": f"{full} [What Stratum Open looks for]({HELP})."},
        "helpUri": HELP,
        "properties": {"tags": [finding.source, finding.category]},
    }


def _result(finding: Finding) -> dict[str, Any]:
    result: dict[str, Any] = {"ruleId": rule_id(finding), "level": LEVEL[finding.severity], "message": {"text": _message(finding)}}
    if finding.path:
        location: dict[str, Any] = {"artifactLocation": {"uri": quote(finding.path), "uriBaseId": "%SRCROOT%"}}
        if finding.line_start is not None:
            # Whole lines, since a finding has no columns: a region without them covers its lines in full.
            # GitHub's table lists the columns as required, but its own examples leave them out.
            location["region"] = {"startLine": finding.line_start, "endLine": finding.line_end or finding.line_start}
        result["locations"] = [{"physicalLocation": location}]
    if finding.fingerprint:
        result["partialFingerprints"] = {"primaryLocationLineHash": finding.fingerprint}
    result["properties"] = {"source": finding.source, "severity": finding.severity}
    return result


def _message(finding: Finding) -> str:
    """The title first: where there's little room, code scanning shows only a message's first sentence."""
    title = " ".join(finding.title.split())
    parts = [title if title.endswith((".", "?", "!")) else f"{title}.", finding.detail, f"Fix: {finding.suggestion}" if finding.suggestion else ""]
    return " ".join(" ".join(part.split()) for part in parts if part.strip())


def _text(log: dict[str, Any]) -> str:
    # ASCII, with the rest escaped: a model's text can hold a lone surrogate, which UTF-8 can't encode.
    return json.dumps(log, indent=2) + "\n"
