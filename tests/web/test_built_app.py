"""The built browser app read against the page's Content-Security-Policy, so a change that would need an inline script
or style, or a font or a script from another origin, fails here and not only in a browser.

It reads the build the app serves: WEB_DIST in the image, or frontend/dist after make frontend-build. In the test
container REQUIRE_WEB_BUILD=1 makes a missing build fail rather than skip."""

import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.web import app as web


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_script = False
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_script = True


@pytest.fixture(scope="module")
def build() -> Path:
    root = web.web_dist()
    if not (root / "index.html").is_file():
        if os.environ.get("REQUIRE_WEB_BUILD") == "1":
            pytest.fail(f"there is no build at {root}, and REQUIRE_WEB_BUILD=1 says the app must have one")
        pytest.skip(f"there is no build at {root}. Run make frontend-build")
    return root


def test_the_page_runs_nothing_inline_and_loads_only_its_own_files(build: Path) -> None:
    page = Tags()
    page.feed((build / "index.html").read_text())
    assert not page.inline_script
    assert "style" not in [tag for tag, _ in page.tags]
    for tag, attrs in page.tags:
        assert "style" not in attrs and not any(name.startswith("on") for name in attrs), (tag, attrs)
    loaded = [attrs.get("src") or attrs.get("href") for tag, attrs in page.tags if tag in ("script", "link")]
    assert "/static/app.js" in loaded and "/static/style.css" in loaded
    for path in loaded:
        assert path and path.startswith("/static/"), path
        assert (build / path.removeprefix("/")).is_file(), path


def test_the_stylesheet_uses_fonts_from_this_origin_and_no_imports(build: Path) -> None:
    css = (build / "static" / "style.css").read_text()
    urls = re.findall(r"url\(\s*['\"]?([^'\")]+)", css)
    fonts = [url for url in urls if url.endswith(".woff2")]
    assert fonts and '"Inter Variable"' in css.replace("'", '"')
    for url in fonts:
        assert url.startswith("/static/") and (build / url.removeprefix("/")).is_file(), url
    # A data: URL is an image the policy allows. A font or a stylesheet in one would be refused.
    assert all(url.startswith(("/static/", "data:image/")) for url in urls), urls
    assert "@import" not in css


def test_the_scripts_are_files_on_this_origin(build: Path) -> None:
    scripts = sorted((build / "static").glob("*.js"))
    assert build / "static" / "app.js" in scripts
    for script in scripts:
        text = script.read_text()
        # Scripts only load more of this build, never a script or a stylesheet from somewhere else.
        assert not re.search(r"""(import\(|src=|href=)\s*["']https?://""", text), script.name
