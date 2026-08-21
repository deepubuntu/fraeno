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

    assert "Catch dangerous robot behavior before deployment." in page
    assert (
        "essentially, dependabot for robots + a security gate before deployment."
    ) in page
    assert (
        "A bad update can make a robot move the wrong way, fail to stop, or become dangerous"
    ) in page
    assert "Fraeno catches software changes that make robots behave dangerously." in page
    assert "An update can look harmless while the robot becomes dangerous." not in page
    assert "© 2026 DeepUbuntu Labs" in page


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


def test_site_shows_verified_proof_without_overclaiming() -> None:
    page = (SITE / "index.html").read_text()

    assert "Stop button" in page
    assert "Pressed" in page
    assert "Robot" in page
    assert "Kept moving" in page
    assert "Fraeno" in page
    assert "Blocked the update" in page
    proof_footer = page.split('class="proof-footer"', 1)[1].split("</a>", 1)[0]
    assert (
        'href="https://github.com/Thabhelo/fraeno-demo-trial/actions/runs/'
        in proof_footer
    )
    assert 'rel="noopener noreferrer"' in proof_footer
    assert "See a real Fraeno check" in proof_footer
    assert "Real robot protected" in page
    assert "Example: the stop button was pressed" in page
    assert (
        "Fraeno checks the robot actions you choose. It cannot promise every possible action is"
        in page
    )


def test_site_never_links_to_private_github_resources() -> None:
    page = (SITE / "index.html").read_text()
    parser = SiteParser()
    parser.feed(page)

    assert "github.com/apps/" not in page
    for target in parser.links:
        if "fraeno-demo-robot" in target:
            continue
        assert "github.com/deepubuntu" not in target, target
        assert "github.com/apps/fraeno" not in target, target

    assert "mailto:" not in page
    assert page.count("data-contact-open") == 3
    assert "Request access" in page
    assert page.count("https://github.com/deepubuntu/fraeno-demo-robot") >= 3
    assert "Try the demo robot" in page
    assert "Install Fraeno" not in page


def test_site_submits_access_requests_through_the_contact_worker() -> None:
    page = (SITE / "index.html").read_text()
    script = (SITE / "site.js").read_text()

    assert 'data-contact-overlay' in page
    assert 'role="dialog"' in page
    assert 'data-contact-form' in page
    assert 'name="website"' in page
    assert 'name="github"' in page
    assert 'tabindex="-1"' in page
    assert '"/api/contact"' in script
    assert "dwell_ms" in script
    assert 'github: fields.get("github")' in script
    assert "thabhelo@deepubuntu.com" in script
    worker = Path(__file__).parents[1] / "contact-worker" / "src" / "index.js"
    assert worker.exists()
    worker_source = worker.read_text()
    assert "MINIMUM_DWELL_MS" in worker_source
    assert "payload.website" in worker_source
    assert worker_source.count("await env.EMAIL.send") == 1
    assert 'subject: "We received your Fraeno access request"' in worker_source
    assert "Fraeno access request from" not in worker_source


def test_admin_console_is_protected_and_uses_product_records() -> None:
    page = (SITE / "admin" / "index.html").read_text()
    script = (SITE / "admin" / "admin.js").read_text()
    styles = (SITE / "admin" / "admin.css").read_text()
    headers = (SITE / "_headers").read_text()
    rules = (ROOT / "deploy" / "firebase" / "firestore.rules").read_text()

    assert "Admin Login" in page
    assert "Sign in to access the admin dashboard" in page
    assert "Email Address" in page
    assert 'type="password"' in page
    assert "Sign In with Email" in page
    assert "Or continue with" in page
    assert "Sign In with Google" in page
    assert "Forgot password?" in page
    assert "← Back to Home" in page
    assert "Admin access only • Unauthorized access is monitored" in page
    assert "Protected by Firebase Authentication" not in page
    assert "sendPasswordResetEmail" in script
    assert "signInWithPopup" in script
    assert "GoogleAuthProvider" in script
    assert "If an account exists for this email" in script
    assert "Paid customers" in script
    assert "fraeno_installations" in script
    assert "fraeno_entitlements" in script
    assert "fraeno_usage" in script
    assert 'fetch("/api/admin/leads"' in script
    assert "token.claims.isAdmin !== true" in script
    assert "--green: #8db61f" in styles
    assert "https://www.gstatic.com" in headers
    assert "script-src 'self' https://apis.google.com" in headers
    assert "frame-src https://accounts.google.com" in headers
    assert "https://fraeno-prod.firebaseapp.com" in headers
    assert "/admin/" in headers and "Cache-Control: no-store" in headers
    public_headers, admin_headers = headers.split("/admin/*", maxsplit=1)
    assert "https://www.gstatic.com" not in public_headers
    assert "https://identitytoolkit.googleapis.com" not in public_headers
    assert "https://www.gstatic.com" in admin_headers
    assert "https://identitytoolkit.googleapis.com" in admin_headers
    assert "Cross-Origin-Opener-Policy: same-origin-allow-popups" in admin_headers
    assert 'type="button"' in page
    assert "data-entitlement-close" in page
    assert "data-entitlement-save" in page
    assert "event.submitter !== entitlementSaveButton" in script
    assert "/admin/admin.css?v=" in page
    assert "/admin/admin.js?v=" in page
    assert "match /fraeno_installations" in rules
    assert "match /fraeno_usage" in rules
    assert "match /fraeno_entitlements" in rules
    assert "allow read, write: if isAdmin();" in rules


def test_privacy_notice_explains_installation_and_usage_metadata() -> None:
    privacy = (SITE / "privacy.html").read_text()

    assert "Fraeno installations" in privacy
    assert "installation ID" in privacy
    assert "basic check activity" in privacy
    assert "do not store repository" in privacy


def test_site_uses_product_motion_without_decorative_media() -> None:
    page = (SITE / "index.html").read_text()
    script = (SITE / "site.js").read_text()
    headers = (SITE / "_headers").read_text()

    hero_video = page.split('data-hero-visual aria-hidden="true">', 1)[1].split(
        "</video>", 1
    )[0]
    assert "autoplay" in hero_video
    assert page.count("autoplay") == 1
    assert page.count('preload="none"') == 2
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
    assert 'src="/site.js?v=a3e1d9cd"' in page
    assert page.count("A bad update can make a robot move the wrong way") == 1
    why_section = page.split('<section class="system-intro section-shell"', 1)[1].split(
        '<section class="proof"', 1
    )[0]
    assert why_section.count(
        "Fraeno catches software changes that make robots behave dangerously."
    ) == 1
    assert 'class="system-outcome"' in why_section
    assert ".system-outcome" in (SITE / "styles.css").read_text()
    assert "grid-column: 2 / -1" in (SITE / "styles.css").read_text()
    assert (
        "grid-template-columns: 2.5rem minmax(14rem, 0.55fr) minmax(28rem, 1.45fr)"
        in (SITE / "styles.css").read_text()
    )
    assert page.count('data-proof-moment>') == 3
    assert "Fraeno catches software changes that make robots behave dangerously." in page
    assert "IntersectionObserver" in script
    assert "updateProof" in script
    assert 'proof.dataset.activeMoment = String(activeIndex + 1)' in script
    assert '--trace-progress' in (SITE / "styles.css").read_text()
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
    primary_navigation = page.split(
        '<nav id="primary-navigation"', 1
    )[1].split("</nav>", 1)[0]
    assert 'href="#why"' in primary_navigation
    assert 'href="#action"' in primary_navigation
    assert 'href="#method"' not in primary_navigation
    assert 'href="#coverage"' not in primary_navigation
    assert 'textLength="1000"' in page
    assert "backdrop-filter: blur(24px)" in styles
    assert "radial-gradient(" in styles
    assert "grid-template-columns: repeat(2, minmax(8rem, 1fr))" in styles
    assert ".footer-word text" in styles


def test_site_keeps_the_full_plain_language_method_illustration() -> None:
    page = (SITE / "index.html").read_text()
    styles = (SITE / "styles.css").read_text()

    assert '<section class="proof" id="method"' in page
    assert "Try the update away from the real robot." in page
    assert "Find the update" in page
    assert "Create a virtual copy of the robot" in page
    assert "Run the existing and updated software" in page
    assert "Detect and block dangerous changes" in page
    assert "without putting the real machine at risk" in page
    assert "minmax(28rem, 1.45fr)" in styles


def test_action_and_supported_systems_follow_the_approved_page_order() -> None:
    page = (SITE / "index.html").read_text()

    action = page.index('<section class="action section-shell"')
    coverage = page.index('<section class="availability section-shell"')
    closing = page.index('<section class="closing" id="access">')

    assert action < coverage < closing
    assert "Fraeno in action" in page
    assert 'class="action-video"' in page
    assert 'src="/assets/fraeno-demo-reel.mp4"' in page
    assert 'poster="/assets/fraeno-demo-poster.jpg"' in page
    assert "Supported systems" in page
    assert "Protect the robot from dangerous software updates." in page


def test_site_keeps_hero_copy_readable_and_centers_the_tablet_footer() -> None:
    page = (SITE / "index.html").read_text()
    styles = (SITE / "styles.css").read_text()
    tablet_footer = styles.split("@media (max-width: 1100px)", 1)[1].split(
        "@media (max-width: 900px)", 1
    )[0]

    assert (
        "Catch dangerous robot behavior before deployment."
        in page
    )
    assert 'class="hero-support"' in page
    assert 'class="hero-aside"' not in page
    assert 'href="/styles.css?v=b20dbb2e"' in page
    assert ".hero-support .round-link" in styles
    assert "padding-top: clamp(11.5rem, 22vh, 14rem)" in styles
    assert "padding-top: 11rem" in styles
    assert "grid-template-columns: 1fr" in tablet_footer
    assert "justify-items: center" in tablet_footer
    assert "text-align: center" in tablet_footer
    assert ".footer-nav > div" in tablet_footer
    assert "align-items: center" in tablet_footer


def test_site_pins_the_three_step_security_story_and_keeps_a_static_fallback() -> None:
    page = (SITE / "index.html").read_text()
    styles = (SITE / "styles.css").read_text()
    script = (SITE / "site.js").read_text()
    reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]

    for moment in (
        "Fraeno is the security gate",
        "between a software update and the robot.",
        "It catches changes that make the robot behave dangerously.",
    ):
        assert moment in page

    assert "height: 280svh" in styles
    assert ".proof-stage" in styles
    assert "position: sticky" in styles
    assert ".proof-moment.is-active" in styles
    assert ".proof-moment.is-next" in styles
    assert ".proof-moment:last-child" in styles
    assert "const momentProgress = Math.min(progress / 0.72, 0.999)" in script
    assert "Math.floor(momentProgress * proofMoments.length)" in script
    assert "const traceProgress = Math.min(Math.max((progress - 0.72) / 0.16" in script
    assert "height: auto" in reduced_motion
    assert "position: static" in reduced_motion
    assert ".proof-result" in reduced_motion
    assert "opacity: 1" in reduced_motion


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


def test_site_has_share_metadata_with_the_fraeno_icon_set() -> None:
    page = (SITE / "index.html").read_text()
    privacy = (SITE / "privacy.html").read_text()
    manifest = json.loads((SITE / "site.webmanifest").read_text())

    for document in (page, privacy):
        assert 'rel="icon" href="/favicon.ico"' in document
        assert 'rel="apple-touch-icon" href="/assets/apple-touch-icon.png"' in document
    assert 'rel="manifest" href="/site.webmanifest"' in page
    assert "https://fraeno.com/assets/social-card-16fc030d.jpg" in page
    assert 'name="twitter:card" content="summary_large_image"' in page
    assert manifest["name"] == "Fraeno"
    assert manifest["display"] == "browser"
    assert manifest["theme_color"] == "#ff6b2c"
    assert [icon["src"] for icon in manifest["icons"]] == [
        "/assets/icon-192.png",
        "/assets/icon-512.png",
    ]

    icon_names = ("favicon-32.png", "apple-touch-icon.png", "icon-192.png", "icon-512.png")
    for name in icon_names:
        assert (SITE / "assets" / name).is_file()
    assert (SITE / "favicon.ico").is_file()
    assert (SITE / "assets" / "social-card-16fc030d.jpg").is_file()


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
        "Fraeno catches software changes that make robots behave dangerously."
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


def test_site_offers_booking_after_a_successful_request() -> None:
    page = (SITE / "index.html").read_text()
    script = (SITE / "site.js").read_text()

    assert 'data-contact-book' in page
    assert 'href="https://calendar.app.google/fB6AtdB5FVSs8YoA9"' in page
    assert 'rel="noopener noreferrer"' in page
    assert "booking.hidden = false" in script


def test_site_ships_discovery_and_privacy_furniture() -> None:
    page = (SITE / "index.html").read_text()
    robots = (SITE / "robots.txt").read_text()
    sitemap = (SITE / "sitemap.xml").read_text()
    llms = (SITE / "llms.txt").read_text()
    privacy = (SITE / "privacy.html").read_text()
    styles = (SITE / "styles.css").read_text()

    assert 'href="/privacy.html"' in page
    assert "ai-train=no" in robots
    assert "Sitemap: https://fraeno.com/sitemap.xml" in robots
    assert "Disallow: /api/" in robots
    assert "https://fraeno.com/privacy.html" in sitemap
    assert "https://fraeno.com/" in llms
    assert "sets no" in privacy and "cookies" in privacy
    assert "legal-wordmark" in privacy
    assert "Product updates" in privacy
    assert "thabhelo@deepubuntu.com" in privacy
    assert "max-width: 70rem" in styles


def test_site_contact_form_keeps_message_optional_with_update_consent() -> None:
    page = (SITE / "index.html").read_text()
    script = (SITE / "site.js").read_text()
    worker = (
        Path(__file__).parents[1] / "contact-worker" / "src" / "index.js"
    ).read_text()

    assert "minlength" not in page
    assert 'name="updates"' in page
    assert "(optional)" not in page
    assert 'fields.get("updates") === "on"' in script
    assert "env.CONTACTS.put" in worker
    assert "message: { min: 0, max: 4000 }" in worker
    assert "github: { min: 1, max: 39 }" in worker
    assert "GITHUB_LOGIN_PATTERN" in worker
    assert "/api/unsubscribe" in worker
    assert "List-Unsubscribe-Post" in worker
    assert "unsubscribe_token" in worker
    assert "&copy; 2026 DeepUbuntu Labs" in worker
