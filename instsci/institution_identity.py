"""Institution name helpers for publisher login flows."""

from __future__ import annotations

from collections.abc import Iterable


def institution_aliases(value: str, extra_aliases: Iterable[str] = ()) -> tuple[str, ...]:
    """Return page-visible aliases for a user-selected institution."""
    raw_aliases = [value, *list(extra_aliases)]
    aliases: list[str] = []
    for raw in raw_aliases:
        alias = str(raw or "").strip()
        if not alias:
            continue
        aliases.append(alias)
        lower = alias.casefold()
        if "openathens" not in lower:
            aliases.append(f"{alias}(OpenAthens)")
            aliases.append(f"{alias} (OpenAthens)")
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def institution_result_selectors(value: str, extra_aliases: Iterable[str] = ()) -> tuple[str, ...]:
    """Build Playwright selectors from institution aliases."""
    selectors: list[str] = []
    for alias in institution_aliases(value, extra_aliases):
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
