"""Config-driven connector bases.

Most portals differ only in selectors, endpoints and vocabulary — not in
control flow. These two classes implement that shared control flow once, so a
new source is usually a YAML file plus a ten-line subclass.
"""

from app.connectors.generic.api_connector import JsonApiConnector
from app.connectors.generic.html_connector import HtmlListingConnector

__all__ = ["HtmlListingConnector", "JsonApiConnector"]
