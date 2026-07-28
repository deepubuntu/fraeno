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
        if tag in {"a", "link", "script", "img"}:
            target = attributes.get("href") or attributes.get("src")
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
        "Fraeno automatically manages and updates robot software dependencies, and tests the\n"
        "            complete robotic system before changes are deployed."
    ) in page
    assert "essentially, dependabot for robots + integration testing." in page
    assert "A passing result means the configured target and declared probes passed." in page


def test_site_uses_light_accessible_presentation_and_plain_punctuation() -> None:
    parser = SiteParser()
    parser.feed((SITE / "index.html").read_text())
    visible_text = " ".join(parser.body_parts)
    styles = (SITE / "styles.css").read_text()

    assert "color-scheme: light" in styles
    assert "prefers-reduced-motion" in styles
    assert "—" not in visible_text
    assert ":" not in visible_text
    assert "A DeepUbuntu product" not in visible_text


def test_site_shows_verified_external_proof_without_overclaiming() -> None:
    page = (SITE / "index.html").read_text()

    assert "Passed in 1m 38s" in page
    assert (
        "https://github.com/deepubuntu/fraeno-onboarding-smoke/actions/runs/30368351839"
        in page
    )
    assert "A design partner running Fraeno on a real robot repository" in page
    assert "It does not\n          claim that every possible robot behavior is safe." in page
