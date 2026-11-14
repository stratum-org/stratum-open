"""Stratum's open core: PR review and triage on a model the maintainers run.

The public API is what this module exports (`__all__`). Code that embeds the
engine, such as Stratum, imports only these names; their signatures are
pinned by `tests/test_public_api.py`, and changing one is a new minor version
(a new major version once 1.0 is out).
"""

__version__ = "0.3.0"

from stratum_open.checks import looks_binary, moved_in  # noqa: E402
from stratum_open.diff import FileDiff, parse_diff  # noqa: E402
from stratum_open.engine import EngineResult, analyze, snippet_of  # noqa: E402
from stratum_open.findings import Finding, placeable  # noqa: E402
from stratum_open.model import Model, Reply  # noqa: E402
from stratum_open.policy import Policy, load_policy, parse_policy  # noqa: E402
from stratum_open.profiles import ModelInfo  # noqa: E402
from stratum_open.track_record import AuthorRecord, author_identity, build_records  # noqa: E402

__all__ = [
    "AuthorRecord",
    "EngineResult",
    "FileDiff",
    "Finding",
    "Model",
    "ModelInfo",
    "Policy",
    "Reply",
    "__version__",
    "analyze",
    "author_identity",
    "build_records",
    "load_policy",
    "looks_binary",
    "moved_in",
    "parse_diff",
    "parse_policy",
    "placeable",
    "snippet_of",
]
