"""The adapter's ``formatted_body`` is sanitized: raw HTML and non-allowlisted link schemes in the
Markdown never reach the Matrix client as live markup (#42667). Standalone sends are delivered by the
gateway-owned adapter in this fork, so they take this same path."""

import pytest

pytest.importorskip("markdown")


def _adapter_html(message):
    from gateway.config import PlatformConfig
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(PlatformConfig(
        enabled=True, token="syt_test_token",
        extra={"homeserver": "https://matrix.example.org", "user_id": "@bot:example.org"}))
    return adapter._build_text_message_content(message)["formatted_body"]


@pytest.mark.parametrize("message", [
    'hi <img src=x onerror="alert(1)"> **bold**',
    "[click](javascript:alert(1))",
    "[click](&#x6a;avascript:alert(1))",
    "<script>alert(1)</script> after",
    '<a href="data:text/html,x">x</a>',
])
def test_formatted_body_is_sanitized(message):
    from html.parser import HTMLParser

    html = _adapter_html(message).lower()
    for needle in ("<img", "onerror", "<script", "javascript:", "data:"):
        assert needle not in html, (needle, html)
    hrefs = []

    class _Hrefs(HTMLParser):  # attribute values arrive entity-decoded, as a client sees them
        def handle_starttag(self, tag, attrs):
            hrefs.extend(v or "" for k, v in attrs if k == "href")

    _Hrefs().feed(html)
    assert not [h for h in hrefs if h.strip().startswith(("javascript:", "data:"))], (hrefs, html)


def test_formatted_body_keeps_safe_formatting():
    html = _adapter_html("# Title\n\n**bold** [site](https://example.com) $x^2$")
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert '<a href="https://example.com">site</a>' in html
    assert 'data-mx-maths="x^2"' in html


@pytest.mark.parametrize("message", [
    "[x](https://e.com/$onmouseover=alert(1)//$)",
    "[x](https://e.com/$ onfocus=alert(1) autofocus tabindex=1 //$)",
    '<a href="https://e.com/$x onclick=alert(1)$">x</a>',
])
def test_tex_in_link_url_cannot_break_out_of_href(message):
    from html.parser import HTMLParser

    html = _adapter_html(message)
    seen = []

    class _Attrs(HTMLParser):
        def handle_starttag(self, tag, attrs):
            seen.append((tag, [k for k, _ in attrs]))

    _Attrs().feed(html)
    for tag, keys in seen:
        assert not [k for k in keys if k.startswith("on") or k in {"autofocus", "tabindex"}], (tag, keys, html)
        if tag == "a":
            assert keys in ([], ["href"]), (keys, html)
    assert "data-mx-maths" not in html, html


def test_tex_in_link_text_still_renders():
    html = _adapter_html("see [the $x^2$ term](https://example.com) and $$y$$")
    assert '<span data-mx-maths="x^2">x^2</span>' in html
    assert '<div data-mx-maths="y">y</div>' in html
    assert 'href="https://example.com"' in html
