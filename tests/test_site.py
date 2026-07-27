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
        self._in_title = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag in {"a", "link", "script", "img"}:
            target = attributes.get("href") or attributes.get("src")
            if target is not None:
                self.links.append(target)
        if tag == "link" and attributes.get("rel") == "canonical":
            self.canonical = attributes.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


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
