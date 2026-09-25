import httpx
import pytest

from app.services.linkedin_post import (
    LinkedInImportError, extract_linkedin_text, import_linkedin_post, normalize_linkedin_url,
)


@pytest.mark.parametrize("url", [
    "http://www.linkedin.com/jobs/view/123", "https://localhost/jobs/view/123",
    "https://www.linkedin.com.evil.test/posts/test", "https://user@www.linkedin.com/posts/test",
    "https://www.linkedin.com:8443/posts/test", "https://www.linkedin.com/in/person",
    "https://www.linkedin.com/jobs/view/../login", "https://www.linkedin.com/jobs/search/",
])
def test_rejects_unapproved_urls(url):
    with pytest.raises(LinkedInImportError):
        normalize_linkedin_url(url)


@pytest.mark.parametrize("path", ["jobs/view/developer-123", "posts/example-activity-123", "feed/update/urn:li:activity:123"])
def test_canonicalizes_supported_urls(path):
    assert normalize_linkedin_url(f"https://fr.linkedin.com/{path}?tracking=1") == f"https://www.linkedin.com/{path}/"


def test_extracts_structured_job_description():
    html = '''<script type="application/ld+json">{"@graph":[{"@type":"JobPosting",
    "title":"Python developer", "description":"<p>Build APIs</p><p>Use SQL</p>"}]}</script>'''
    assert extract_linkedin_text(html) == "Python developer\nBuild APIs\nUse SQL"


def test_extracts_public_post_without_page_navigation():
    assert extract_linkedin_text('<nav>Sign in</nav><div class="attributed-text-segment-list__content">Hiring Java developers</div>') == "Hiring Java developers"


def test_login_page_is_not_used_as_job_text():
    with pytest.raises(LinkedInImportError):
        extract_linkedin_text('<h1>Sign in</h1><p>Join LinkedIn</p>')


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(302, ""), (403, ""), (200, "x" * (2 * 1024 * 1024 + 1))])
async def test_rejects_redirects_blocks_and_large_pages(monkeypatch, status, body):
    original = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=body, headers={"content-type": "text/html", "location": "http://localhost"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(LinkedInImportError):
        await import_linkedin_post("https://www.linkedin.com/posts/test")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_import_success(monkeypatch):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text='<div class="show-more-less-html__markup">Python and SQL</div>', headers={"content-type": "text/html"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    result = await import_linkedin_post("https://www.linkedin.com/jobs/view/123?tracking=1")
    assert result == {"text": "Python and SQL", "source_url": "https://www.linkedin.com/jobs/view/123/"}
