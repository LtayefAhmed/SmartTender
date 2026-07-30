"""Parsing: HTML/JSON in, plain values out. No network, no I/O, no side effects.

Because nothing here touches the network, every parser is testable against a
saved fixture — which is exactly how the regression suite pins portal markup.
"""

from app.connectors.parsing.normalizers import (
    normalize_date,
    normalize_money,
    normalize_text,
    parse_bool,
)
from app.connectors.parsing.selectors import SelectorEngine, extract_json_path

__all__ = [
    "SelectorEngine",
    "extract_json_path",
    "normalize_date",
    "normalize_money",
    "normalize_text",
    "parse_bool",
]
