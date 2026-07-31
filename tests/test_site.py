import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

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
            asset_path = urlsplit(target).path.removeprefix("/")
            assert (SITE / asset_path).is_file(), target


def test_site_preserves_approved_product_copy() -> None:
    page = (SITE / "index.html").read_text()

    assert (
        "Fraeno updates the software inside robots, then tests that they still work."
    ) in page
    assert "essentially, dependabot for robots + integration testing." in page
    assert (
        "A pass means the robot checks you chose worked. It does not promise that every possible"
        in page
    )


def test_site_uses_light_accessible_presentation_and_plain_punctuation() -> None:
    parser = SiteParser()
    parser.feed((SITE / "index.html").read_text())
    visible_text = " ".join(parser.body_parts)
    styles = (SITE / "styles.css").read_text()

    assert "color-scheme: light" in styles
    assert "prefers-reduced-motion" in styles
    assert '--font: "Inter Tight"' in styles
    assert "font-weight: 420" in styles
    assert "--orange: #ff6333" in styles
    assert "backdrop-filter: blur(20px)" in styles
    assert "https://fonts." not in styles
    assert "8.5rem" not in styles
    assert "#071229" not in styles
    assert "—" not in visible_text
    assert ":" not in visible_text
    assert "A DeepUbuntu product" not in visible_text
    assert "brand-mark" not in (SITE / "index.html").read_text()


def test_site_shows_verified_external_proof_without_overclaiming() -> None:
    page = (SITE / "index.html").read_text()

    assert "Sent 20.6 readings each second" in page
    assert "Robot controller" in page
    assert "Received nothing" in page
    assert "Movement commands" in page
    assert "Stopped" in page
    assert (
        "https://github.com/deepubuntu/fraeno-onboarding-smoke/actions/runs/30368351839"
        in page
    )
    assert "Production test" in page
    assert (
        "A pass means the robot checks you chose worked. It does not promise that every possible"
        in page
    )


def test_site_uses_product_motion_without_decorative_media() -> None:
    page = (SITE / "index.html").read_text()
    script = (SITE / "site.js").read_text()
    headers = (SITE / "_headers").read_text()

    video_markup = (
        "<video\n"
        "              autoplay\n"
        "              muted\n"
        "              loop\n"
        "              playsinline"
    )
    assert video_markup in page
    assert 'poster="/assets/robot-system-poster.webp?v=22eddc0b"' in page
    assert 'src="/assets/robot-system-loop.mp4?v=e7e40370"' in page
    assert "data-section-video" in page
    assert 'src="/assets/fraeno-hero-loop-952e20e2.mp4"' in page
    assert 'poster="/assets/fraeno-hero-poster-fd73bfa6.jpg"' in page
    assert "data-hero-video" in page
    assert 'src="/assets/fraeno-robot-arm.webp"' in page
    assert 'href="/assets/inter-tight-latin.woff2"' in page
    assert "data-hero-visual" in page
    assert "data-trace" in page
    assert "the robot can still do the same jobs" in page
    assert "IntersectionObserver" in script
    assert "configureSectionVideo" in script
    assert "configureHeroVideo" in script
    hero_tag = re.search(r"<video\s+(.*?)data-hero-video\s*>", page, re.DOTALL)
    assert hero_tag is not None
    hero_attributes = hero_tag.group(1)
    for required_attribute in ("autoplay", "muted", "loop", "playsinline"):
        assert required_attribute in hero_attributes
    assert "controls" not in hero_attributes
    assert 'preload="auto"' in hero_attributes
    assert "heroVideo.controls = false" in script
    assert "::-webkit-media-controls" in (SITE / "styles.css").read_text()
    assert "requestAnimationFrame" in script
    assert "prefers-reduced-motion: reduce" in (SITE / "styles.css").read_text()
    assert "media-src" not in headers
    assert "font-src 'self'" in headers
    assert "/assets/*" in headers


def test_site_adapts_the_reference_navigation_and_complete_footer() -> None:
    page = (SITE / "index.html").read_text()
    styles = (SITE / "styles.css").read_text()

    assert 'class="menu-button"' in page
    assert 'class="footer-main"' in page
    assert 'class="footer-word"' in page
    assert 'class="footer-nav"' in page
    assert 'href="#method"' in page
    assert 'href="#coverage"' in page
    assert 'textLength="1000"' in page
    assert "backdrop-filter: blur(24px)" in styles
    assert "radial-gradient(" in styles
    assert "grid-template-columns: repeat(3, minmax(8rem, 1fr))" in styles
    assert ".footer-word text" in styles


def test_site_keeps_the_full_plain_language_method_illustration() -> None:
    page = (SITE / "index.html").read_text()
    styles = (SITE / "styles.css").read_text()

    assert '<section class="method section-shell" id="method"' in page
    assert "Test the whole robot before the update goes live." in page
    assert "Find one update" in page
    assert "Watch the robot work now" in page
    assert "Try the updated software" in page
    assert "Check that the robot still works" in page
    assert "minmax(26rem, 1.28fr)" in styles


def test_site_keeps_hero_copy_readable_and_centers_the_tablet_footer() -> None:
    page = (SITE / "index.html").read_text()
    styles = (SITE / "styles.css").read_text()
    tablet_footer = styles.split("@media (max-width: 1100px)", 1)[1].split(
        "@media (max-width: 900px)", 1
    )[0]

    assert (
        "Stop dangerous robot behavior before deployment."
        in page
    )
    assert 'class="hero-support"' in page
    assert 'class="hero-aside"' not in page
    assert ".hero-support .round-link" in styles
    assert "grid-template-columns: 1fr" in tablet_footer
    assert "justify-items: center" in tablet_footer
    assert "text-align: center" in tablet_footer
    assert ".footer-nav > div" in tablet_footer
    assert "align-items: center" in tablet_footer


def test_site_bundles_original_visual_and_self_hosted_font() -> None:
    robot = SITE / "assets" / "fraeno-robot-arm.webp"
    font = SITE / "assets" / "inter-tight-latin.woff2"
    license_file = SITE / "assets" / "INTER-TIGHT-OFL.txt"
    system_video = SITE / "assets" / "robot-system-loop.mp4"
    system_poster = SITE / "assets" / "robot-system-poster.webp"
    hero_video = SITE / "assets" / "fraeno-hero-loop-952e20e2.mp4"
    hero_poster = SITE / "assets" / "fraeno-hero-poster-fd73bfa6.jpg"

    assert robot.is_file()
    assert robot.stat().st_size < 100_000
    assert font.is_file()
    assert license_file.is_file()
    assert system_video.is_file()
    assert system_video.stat().st_size < 500_000
    assert system_poster.is_file()
    assert system_poster.stat().st_size < 50_000
    assert hero_video.is_file()
    assert hero_video.stat().st_size < 1_000_000
    assert hero_poster.is_file()
    assert hero_poster.stat().st_size < 100_000


def test_site_has_complete_share_and_install_metadata() -> None:
    page = (SITE / "index.html").read_text()
    manifest = json.loads((SITE / "site.webmanifest").read_text())

    assert 'rel="apple-touch-icon" href="/assets/apple-touch-icon.png"' in page
    assert 'rel="manifest" href="/site.webmanifest"' in page
    assert 'property="og:image" content="https://fraeno.com/assets/social-card.jpg"' in page
    assert 'name="twitter:card" content="summary_large_image"' in page
    assert manifest["name"] == "Fraeno"
    assert manifest["display"] == "browser"
    assert manifest["theme_color"] == "#ff6b2c"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}

    for name in ("apple-touch-icon.png", "icon-192.png", "icon-512.png", "social-card.jpg"):
        assert (SITE / "assets" / name).is_file()


def test_structured_product_metadata_is_truthful() -> None:
    page = (SITE / "index.html").read_text()
    headers = (SITE / "_headers").read_text()
    match = re.search(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        page,
        re.DOTALL,
    )

    assert match is not None
    metadata = json.loads(match.group(1))
    assert metadata["name"] == "Fraeno"
    assert metadata["url"] == "https://fraeno.com/"
    assert metadata["description"] == (
        "Catch dangerous changes in robot behavior before software reaches the real robot."
    )
    assert "offers" not in metadata

    raw_match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert raw_match is not None
    digest = base64.b64encode(
        hashlib.sha256(raw_match.group(1).encode()).digest()
    ).decode()
    assert f"'sha256-{digest}'" in headers
