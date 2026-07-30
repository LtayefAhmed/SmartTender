"""Config-driven extraction from HTML and JSON.

Selectors are data, never code. The mini-syntax is intentionally tiny — it has
to be writable by whoever is fixing a broken portal at 9am, not only by the
person who wrote the connector:

    ``td.title``                text of the first match
    ``td.title a@href``         value of an attribute
    ``h1.new, h1.old``          fallbacks, tried left to right
    ``xpath://td[@class='x']``  XPath escape hatch for the awkward cases

The comma-fallback form is what makes a portal redesign survivable: add the new
selector next to the old one, deploy the YAML, and the connector works against
both the cached and the updated markup.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.exceptions import ParsingError

__all__ = ["SelectorEngine", "extract_json_path", "parse_html"]

_ATTR_SUFFIX = re.compile(r"^(?P<selector>.+?)@(?P<attr>[\w:-]+)$")
_XPATH_PREFIX = "xpath:"
#: ``documents[].url`` — collect ``url`` from every element of ``documents``.
_JSON_LIST_MARKER = "[]"


def parse_html(markup: str | bytes, *, parser: str = "lxml") -> BeautifulSoup:
    """Build a soup, falling back to the stdlib parser if lxml is unavailable."""
    try:
        return BeautifulSoup(markup, parser)
    except Exception:
        return BeautifulSoup(markup, "html.parser")


class SelectorEngine:
    """Applies the selector mini-syntax to a parsed document."""

    def __init__(self, soup: BeautifulSoup | Tag) -> None:
        self.root = soup

    # ------------------------------------------------------------------
    @staticmethod
    def _split_alternatives(spec: str) -> list[str]:
        """Split on commas that separate alternatives.

        Commas inside brackets or quotes belong to the selector itself
        (``td[data-x="a,b"]``, ``:is(a, b)``) and must not split it.
        """
        parts: list[str] = []
        depth = 0
        quote: str | None = None
        current: list[str] = []
        for char in spec:
            if quote:
                current.append(char)
                if char == quote:
                    quote = None
                continue
            if char in "\"'":
                quote = char
                current.append(char)
            elif char in "([":
                depth += 1
                current.append(char)
            elif char in ")]":
                depth = max(0, depth - 1)
                current.append(char)
            elif char == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if current:
            parts.append("".join(current).strip())
        return [p for p in parts if p]

    @staticmethod
    def _parse_spec(spec: str) -> tuple[str, str | None]:
        match = _ATTR_SUFFIX.match(spec.strip())
        if match:
            return match.group("selector").strip(), match.group("attr")
        return spec.strip(), None

    def _find_all(self, selector: str) -> list[Tag]:
        if selector.startswith(_XPATH_PREFIX):
            return self._xpath(selector[len(_XPATH_PREFIX) :])
        try:
            return list(self.root.select(selector))
        except Exception:
            return []

    def _xpath(self, expression: str) -> list[Tag]:
        """XPath support via lxml, re-parsing the current subtree."""
        try:
            from lxml import etree
            from lxml import html as lxml_html
        except ImportError:  # pragma: no cover
            return []
        try:
            tree = lxml_html.fromstring(str(self.root))
            found = tree.xpath(expression)
        except (etree.XPathError, etree.ParserError, ValueError):
            return []
        results: list[Tag] = []
        for node in found:
            if isinstance(node, str):
                results.append(BeautifulSoup(f"<span>{node}</span>", "html.parser").span)  # type: ignore[arg-type]
            else:
                results.append(
                    parse_html(lxml_html.tostring(node, encoding="unicode"), parser="html.parser")
                )
        return results

    @staticmethod
    def _value(node: Tag, attribute: str | None) -> str | None:
        if attribute is None:
            text = node.get_text(" ", strip=True)
            return text or None
        raw = node.get(attribute)
        if raw is None:
            return None
        if isinstance(raw, list):  # multi-valued attributes such as class
            raw = " ".join(raw)
        value = str(raw).strip()
        return value or None

    # ------------------------------------------------------------------
    def get(self, spec: str | None, default: str | None = None) -> str | None:
        """First non-empty value across the alternatives."""
        if not spec:
            return default
        for alternative in self._split_alternatives(spec):
            selector, attribute = self._parse_spec(alternative)
            for node in self._find_all(selector):
                value = self._value(node, attribute)
                if value:
                    return value
        return default

    def get_all(self, spec: str | None) -> list[str]:
        """Every value from the first alternative that matches anything.

        Deliberately not the union across alternatives: alternatives are
        *fallbacks* describing the same data in different markup versions, so
        unioning them would duplicate every value on a page that happens to
        match both.
        """
        if not spec:
            return []
        for alternative in self._split_alternatives(spec):
            selector, attribute = self._parse_spec(alternative)
            values = [
                value
                for node in self._find_all(selector)
                if (value := self._value(node, attribute))
            ]
            if values:
                return values
        return []

    def nodes(self, spec: str | None) -> list[SelectorEngine]:
        """Sub-engines scoped to each matching element (listing rows)."""
        if not spec:
            return []
        for alternative in self._split_alternatives(spec):
            selector, _ = self._parse_spec(alternative)
            found = self._find_all(selector)
            if found:
                return [SelectorEngine(node) for node in found]
        return []

    def exists(self, spec: str | None) -> bool:
        if not spec:
            return False
        return any(
            self._find_all(self._parse_spec(alternative)[0])
            for alternative in self._split_alternatives(spec)
        )

    def require(self, spec: str, *, what: str, url: str | None = None) -> None:
        """Assert a guard selector matches, else raise ``SelectorBrokenError``.

        This is the difference between "the portal published nothing" and "we
        have been silently blind for a week". Guard selectors are the single
        highest-value alerting signal the platform has.
        """
        from app.core.exceptions import SelectorBrokenError

        if not self.exists(spec):
            raise SelectorBrokenError(
                f"Guard selector for {what} matched nothing — the page markup has changed.",
                selector=spec,
                url=url,
            )

    def extract(self, mapping: dict[str, str], *, multi: set[str] | None = None) -> dict[str, Any]:
        """Apply a whole ``{field: selector}`` block at once."""
        multi = multi or set()
        result: dict[str, Any] = {}
        for field, spec in mapping.items():
            if not spec:
                continue
            result[field] = self.get_all(spec) if field in multi else self.get(spec)
        return result

    @property
    def text(self) -> str:
        return self.root.get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def extract_json_path(payload: Any, path: str | None, default: Any = None) -> Any:
    """Read a dotted path out of a JSON structure.

    Supports numeric indices (``items.0.name``) and the list-projection marker
    (``documents[].url`` -> every ``url`` in ``documents``), which together
    cover essentially every response shape a procurement API produces.
    """
    if not path:
        return default
    node: Any = payload
    for segment in path.split("."):
        if node is None:
            return default
        if segment.endswith(_JSON_LIST_MARKER):
            key = segment[: -len(_JSON_LIST_MARKER)]
            container = node.get(key) if isinstance(node, dict) else node
            if not isinstance(container, list):
                return default
            node = container
            continue
        if isinstance(node, list):
            # A numeric segment indexes the list; anything else means a
            # projection is in progress and the segment maps over the elements.
            if segment.isdigit():
                index = int(segment)
                if index >= len(node):
                    return default
                node = node[index]
                continue
            node = [
                item.get(segment) if isinstance(item, dict) else None
                for item in node
            ]
            node = [item for item in node if item is not None]
            continue
        if isinstance(node, dict):
            if segment not in node:
                return default
            node = node[segment]
            continue
        if segment.isdigit():
            try:
                node = node[int(segment)]
                continue
            except (IndexError, TypeError, KeyError):
                return default
        return default
    return default if node is None else node


def require_json_items(payload: Any, path: str | None, *, connector: str, url: str) -> list[Any]:
    """Read the item list from an API response, or fail loudly.

    An API that silently changes its envelope is the JSON equivalent of a
    broken selector, and deserves the same alert.
    """
    items = extract_json_path(payload, path) if path else payload
    if items is None:
        raise ParsingError(
            "API response does not contain the configured items path.",
            connector=connector,
            url=url,
            context={"items_path": path},
        )
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ParsingError(
            "API items path did not resolve to a list.",
            connector=connector,
            url=url,
            context={"items_path": path, "resolved_type": type(items).__name__},
        )
    return items
