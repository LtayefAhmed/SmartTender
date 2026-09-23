"""Import public LinkedIn job descriptions without login or browser sessions."""

import json
import re
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup


class LinkedInImportError(ValueError):
    pass


def normalize_linkedin_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        valid = (
            parts.scheme == "https"
            and parts.hostname in {"linkedin.com", "www.linkedin.com", "fr.linkedin.com"}
            and not parts.username and not parts.password
            and parts.port in {None, 443}
        )
    except ValueError:
        valid = False
    if not valid:
        raise LinkedInImportError("Utilisez un lien HTTPS vers une offre ou une publication LinkedIn.")
    path = parts.path.rstrip("/")
    if not (
        re.fullmatch(r"/jobs/view/[A-Za-z0-9_-]+", path)
        or re.fullmatch(r"/posts/[A-Za-z0-9_-]+", path)
        or re.fullmatch(r"/feed/update/urn:li:activity:\d+", path)
    ):
        raise LinkedInImportError("Copiez le lien d'une offre ou d'une publication, pas celui d'un profil ou d'une recherche.")
    # Fixed destination, no caller-controlled query, port, credentials or redirects.
    return "https://www.linkedin.com" + path + "/"


def extract_linkedin_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script[type='application/ld+json']"):
        try:
            pending = [json.loads(node.string or node.get_text())]
        except (ValueError, TypeError):
            continue
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                kind = item.get("@type", [])
                kinds = [kind] if isinstance(kind, str) else kind
                if isinstance(kinds, list) and "JobPosting" in kinds:
                    description = item.get("description")
                    if isinstance(description, str) and description.strip():
                        title = item.get("title", "")
                        text = BeautifulSoup(description, "html.parser").get_text("\n", strip=True)
                        return (str(title) + "\n" + text).strip()[:50000]
                pending.extend(v for v in item.values() if isinstance(v, (dict, list)))
    for selector in (".show-more-less-html__markup", ".feed-shared-update-v2__description", ".attributed-text-segment-list__content"):
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            return node.get_text("\n", strip=True)[:50000]
    raise LinkedInImportError("Le texte de cette publication n'est pas accessible. Ouvrez LinkedIn et utilisez « Coller le texte ».")


async def import_linkedin_post(url: str) -> dict[str, str]:
    source = normalize_linkedin_url(url)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", source) as response:
                if response.status_code != 200:
                    raise LinkedInImportError("LinkedIn bloque l'accès ou demande une connexion. Utilisez « Coller le texte ».")
                if "text/html" not in response.headers.get("content-type", ""):
                    raise LinkedInImportError("Ce lien ne renvoie pas une publication lisible.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise LinkedInImportError("La page est trop volumineuse. Utilisez « Coller le texte ».")
        return {"text": extract_linkedin_text(content.decode("utf-8", errors="replace")), "source_url": source}
    except httpx.HTTPError as exc:
        raise LinkedInImportError("LinkedIn ne répond pas. Réessayez ou utilisez « Coller le texte ».") from exc
