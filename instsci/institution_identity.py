"""Institution name helpers for publisher login flows."""

from __future__ import annotations


def is_tsinghua_institution(value: str) -> bool:
    """Return whether the configured institution text refers to Tsinghua."""
    text = str(value or "").strip().casefold()
    return "tsinghua" in text or "qinghua" in text or "\u6e05\u534e" in text


def institution_aliases(value: str) -> tuple[str, ...]:
    """Return page-visible aliases for a user-selected institution."""
    query = str(value or "").strip()
    if not query:
        return ()

    aliases = [query]
    if is_tsinghua_institution(query):
        aliases.extend(
            [
                "Tsinghua University(OpenAthens)",
                "Tsinghua University (OpenAthens)",
                "Tsinghua University",
                "Tsinghua",
                "\u6e05\u534e\u5927\u5b66",
                "\u6e05\u534e",
            ]
        )
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def institution_result_selectors(value: str) -> tuple[str, ...]:
    """Build Playwright selectors from institution aliases."""
    selectors: list[str] = []
    for alias in institution_aliases(value):
        literal = alias.replace("\\", "\\\\").replace("'", "\\'")
        selectors.extend(
            [
                f"text={alias}",
                f"button:has-text('{literal}')",
                f"a:has-text('{literal}')",
                f"[role='button']:has-text('{literal}')",
                f"[role='option']:has-text('{literal}')",
                f"li:has-text('{literal}')",
                f"div:has-text('{literal}')",
            ]
        )
    return tuple(dict.fromkeys(selectors))
