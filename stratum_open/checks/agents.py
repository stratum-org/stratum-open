"""Text aimed at the AI tools that read a repo rather than at people: changes
to the files that steer coding agents, and characters a reviewer can't see.

Both are ways to get instructions past a human reviewer. An agent file says
what an assistant should do whenever it works in this repo; invisible
characters can hide such instructions (or reorder code, "Trojan Source")
inside a line that looks harmless.
"""

from __future__ import annotations

import unicodedata

from stratum_open.diff import FileDiff
from stratum_open.findings import Finding
from stratum_open.policy import matches_any
from stratum_open.util import count

AGENT_FILES = (
    "**/AGENTS.md", "**/CLAUDE.md", "**/CLAUDE.local.md", "**/GEMINI.md",
    "**/.cursorrules", "**/.cursor/rules/**", "**/.windsurfrules", "**/.windsurf/rules/**",
    "**/.clinerules", "**/.clinerules/**", "**/.roomodes", "**/.roo/**",
    "**/.aider.conf.yml", "**/.mcp.json", "**/.vscode/mcp.json", "**/.cursor/mcp.json",
    "**/.claude/**", "**/.continue/**", "**/.gemini/**",
    ".github/copilot-instructions.md", ".github/instructions/**", ".github/prompts/**",
)


def check_instruction_files(files: list[FileDiff]) -> list[Finding]:
    findings = []
    for file in files:
        changed = [path for path in (file.path, file.old_path) if path and _agent_file(path)]
        if not changed:
            continue
        findings.append(Finding(
            title="Coding-agent instructions changed",
            detail=(
                f"`{changed[0]}` tells AI coding tools (Copilot, Claude Code, Cursor and others) how to work in "
                "this repo, and they follow it without asking. A change here can make every later "
                "AI-assisted change run commands, add dependencies or skip checks, so a maintainer should read it."
            ),
            severity="high",
            category="security",
            path=file.path,
            suggestion="Read the whole change as instructions to an assistant: what would it do differently now?",
            source="check",
            rule="agents/instructions",
            confidence=1.0,
            needs_human=True,
        ))
    return findings


# Matched without regard to case: on macOS and Windows `agents.md` is the
# same file, and some tools look for either spelling.
_AGENT_PATTERNS = tuple(pattern.lower() for pattern in AGENT_FILES)


def _agent_file(path: str) -> bool:
    return matches_any(path.lower(), _AGENT_PATTERNS)


# ── invisible characters ─────────────────────────────────────────────────

_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
_ZERO_WIDTH = {0x200B, 0x200C, 0x200D}
_WORD_JOINER = 0x2060
_BOM = 0xFEFF
_TAGS = range(0xE0000, 0xE0080)
_PER_FILE = 3  # findings per file; the last one lists the remaining lines


def check_hidden_unicode(files: list[FileDiff]) -> list[Finding]:
    findings = []
    for file in files:
        hits = [(number, found) for number, text in file.added() if (found := hidden_characters(text, number == 1))]
        for index, (number, found) in enumerate(hits[:_PER_FILE]):
            detail = (
                f"Line {number} has characters that don't show up in a review: {_describe(found)}. They can make "
                "code run differently from how it reads, or hide instructions that an AI tool will still follow."
            )
            rest = hits[_PER_FILE:]
            if index == _PER_FILE - 1 and rest:
                more = ", ".join(str(n) for n, _ in rest[:10]) + (", …" if len(rest) > 10 else "")
                detail += f" {len(rest)} more added lines in this file have them too (lines {more})."
            findings.append(Finding(
                title="Hidden Unicode characters",
                detail=detail,
                severity="high",
                category="security",
                path=file.path,
                line_start=number,
                line_end=number,
                suggestion="Remove them unless they're needed in this text (for example joiners inside emoji or some scripts).",
                source="check",
                rule="unicode/hidden",
                confidence=0.95,
                needs_human=True,
            ))
    return findings


def hidden_characters(text: str, first_line: bool = False) -> list[str]:
    """The invisible characters in a line, in order. A single zero-width
    joiner or space between two non-ASCII characters is left out: emoji
    sequences and scripts like Persian and the Indic ones need it there."""
    found = []
    for index, char in enumerate(text):
        code = ord(char)
        if code == _BOM:
            if not (first_line and index == 0):
                found.append(char)
        elif code in _BIDI or code == _WORD_JOINER or code in _TAGS:
            found.append(char)
        elif code in _ZERO_WIDTH:
            before = text[index - 1] if index else " "
            after = text[index + 1] if index + 1 < len(text) else " "
            if before.isascii() or after.isascii() or ord(before) in _ZERO_WIDTH or ord(after) in _ZERO_WIDTH:
                found.append(char)
    return found


def _describe(found: list[str]) -> str:
    tags = sum(1 for char in found if ord(char) in _TAGS)
    names = []
    for char in dict.fromkeys(char for char in found if ord(char) not in _TAGS):
        names.append(f"U+{ord(char):04X} {unicodedata.name(char, '')}".strip())
    if tags:
        names.append(f"{count(tags, 'Unicode tag character')} (U+E0000–U+E007F), which can spell out hidden text")
    return ", ".join(names)
