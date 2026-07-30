from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[1]
SITE = ROOT / "site"


class SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.canonical: str | None = None
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self._in_title = False
        self._in_body = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "body":
            self._in_body = True
        if tag in {"a", "link", "script", "img", "source", "video"}:
            target = (
                attributes.get("href")
                or attributes.get("src")
                or attributes.get("poster")
            )
            if target is not None:
                self.links.append(target)
        if tag == "link" and attributes.get("rel") == "canonical":
            self.canonical = attributes.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "body":
            self._in_body = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_body:
            self.body_parts.append(data)


def test_site_has_expected_identity_and_local_assets() -> None:
    index = SITE / "index.html"
    parser = SiteParser()
    parser.feed(index.read_text())

    assert "Fraeno" in "".join(parser.title_parts)
    assert parser.canonical == "https://fraeno.com/"

    for target in parser.links:
        if target.startswith("/") and target != "/":
            assert (SITE / target.removeprefix("/")).is_file(), target


def test_site_preserves_approved_product_copy() -> None:
    page = (SITE / "index.html").read_text()

    assert (
        "Fraeno updates the software inside robots, then tests that they still work."
    ) in page
    assert "essentially, dependabot for robots + integration testing." in page
    assert (
        "A pass covers the behavior your team configures, not every possible robot behavior."
        in page
    )


def test_site_uses_light_accessible_presentation_and_plain_punctuation() -> None:
    parser = SiteParser()
    parser.feed((SITE / "index.html").read_text())
    visible_text = " ".join(parser.body_parts)
    styles = (SITE / "styles.css").read_text()

    assert "color-scheme: light" in styles
    assert "prefers-reduced-motion" in styles
    assert "--orange: #ff6b2c" in styles
    assert "backdrop-filter: blur(24px)" in styles
    assert "https://fonts." not in styles
    assert "#071229" not in styles
    assert "—" not in visible_text
    assert ":" not in visible_text
    assert "A DeepUbuntu product" not in visible_text
    assert "brand-mark" not in (SITE / "index.html").read_text()


def test_site_shows_verified_external_proof_without_overclaiming() -> None:
    page = (SITE / "index.html").read_text()

    assert "20.6 Hz" in page
    assert "0 Hz" in page
    assert (
        "https://github.com/deepubuntu/fraeno-onboarding-smoke/actions/runs/30368351839"
        in page
    )
    assert "Real production test" in page
    assert (
        "A pass covers the behavior your team configures, not every possible robot behavior."
        in page
    )


def test_site_uses_product_motion_without_decorative_media() -> None:
    page = (SITE / "index.html").read_text()
    script = (SITE / "site.js").read_text()
    headers = (SITE / "_headers").read_text()

    assert "<video" not in page
    assert 'data-demo-state="baseline"' in page
    assert 'data-demo-state="updated"' in page
    assert "setDemoState" in script
    assert "IntersectionObserver" in script
    assert "prefers-reduced-motion: reduce" in (SITE / "styles.css").read_text()
    assert "media-src" not in headers
    assert "/assets/*" not in headers
