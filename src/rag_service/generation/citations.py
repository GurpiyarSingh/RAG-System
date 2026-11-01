"""Parse and validate ``[n]`` citation markers in generated answers.

Models occasionally cite sources that don't exist ("[7]" when only 5 were given).
Those markers are reported as invalid and stripped from non-streamed answers, so a
UI never renders a citation link that leads nowhere.

Supported marker forms: ``[1]``, ``[1][2]`` and ``[1, 2]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


@dataclass(frozen=True, slots=True)
class CitationCheck:
    used: list[int]  # valid source numbers, in order of first appearance
    invalid: list[int]  # cited numbers with no matching source


def check_citations(answer: str, source_count: int) -> CitationCheck:
    used: list[int] = []
    invalid: list[int] = []
    for match in _MARKER.finditer(answer):
        for raw in match.group(1).split(","):
            n = int(raw)
            target = used if 1 <= n <= source_count else invalid
            if n not in target:
                target.append(n)
    return CitationCheck(used=used, invalid=invalid)


def strip_invalid(answer: str, source_count: int) -> str:
    """Remove references to non-existent sources; drop markers that become empty."""

    def fix(match: re.Match[str]) -> str:
        valid = [n for n in (int(x) for x in match.group(1).split(",")) if 1 <= n <= source_count]
        return f"[{', '.join(map(str, valid))}]" if valid else ""

    return _MARKER.sub(fix, answer)
