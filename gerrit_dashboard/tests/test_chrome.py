"""Page chrome: optional TLC branding, the dashboard's display extras,
and the version in the footer."""

import re

import pytest

from conftest import mk_bundle, mk_change

from gerrit_dashboard import __version__
from gerrit_dashboard import app as app_mod
from gerrit_dashboard.app import create_app
from gerrit_dashboard.classify import build_snapshot
from gerrit_dashboard.config import Config
from gerrit_dashboard.store import user_store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    class DeadResolver:
        def __init__(self, *a, **k):
            self.rest = self
            self.username = "alex"

        def get(self, *a, **k):
            raise ConnectionError("offline test")

    monkeypatch.setattr(app_mod, "GerritCommentsClient", DeadResolver)


def client(tmp_path, **over):
    cfg = Config()
    cfg.data_dir = tmp_path
    for k, v in over.items():
        setattr(cfg, k, v)
    store = user_store(tmp_path, "alex")
    store.save_snapshot(build_snapshot(mk_bundle([(mk_change(), {"mine"})]), cfg))
    app = create_app(cfg, start_refresher=False)
    app.config.update(TESTING=True)
    return app.test_client()


def board(tmp_path, **over):
    return client(tmp_path, **over).get("/alex/").get_data(as_text=True)


class TestBranding:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("GD_TLC_BRANDING", raising=False)
        assert Config.from_env().tlc_branding is False

    def test_env_turns_it_on(self, monkeypatch):
        monkeypatch.setenv("GD_TLC_BRANDING", "1")
        assert Config.from_env().tlc_branding is True

    @pytest.mark.parametrize("page", ["/", "/alex/"])
    def test_unbranded_pages_carry_no_mark_and_no_favicon(self, tmp_path, page):
        html = client(tmp_path).get(page).get_data(as_text=True)
        assert "tlc-brand" not in html
        assert "gerrit-dashboard.svg" not in html
        assert 'rel="icon"' not in html
        assert 'class="gd-brand"' in html

    @pytest.mark.parametrize("page", ["/", "/alex/"])
    def test_branded_pages_show_the_mark(self, tmp_path, page):
        html = client(tmp_path, tlc_branding=True).get(page).get_data(as_text=True)
        assert 'class="tlc-brand"' in html
        assert 'rel="icon"' in html

    def test_static_export_follows_the_switch(self, tmp_path):
        from flask import render_template
        cfg = Config(); cfg.data_dir = tmp_path
        snap = build_snapshot(mk_bundle([(mk_change(), {"mine"})]), cfg)
        for branding, expect in ((False, False), (True, True)):
            cfg.tlc_branding = branding
            app = create_app(cfg, start_refresher=False)
            with app.test_request_context():
                html = render_template("dashboard.html", snapshot=snap, refreshing=False,
                                       error=None, form_error="", static_mode=True,
                                       gerrit_url="https://x", user="alex")
            assert ("data:image/svg+xml" in html) is expect


class TestDisplayExtras:
    def test_extras_load_after_the_tlc_control(self, tmp_path):
        html = board(tmp_path)
        tlc = html.index("tlc-display.js")
        extras = html.index('KEY = "gd-display"')
        assert tlc < extras

    def test_smallest_and_font_rules_present(self, tmp_path):
        html = board(tmp_path)
        assert 'html:root[data-gd-size="xs"] { font-size: 87.5%; }' in html
        assert 'html[data-gd-font="classic"]' in html
        assert '"Smallest"' in html and ">Classic<" in html

    def test_settings_applied_before_paint(self, tmp_path):
        # The pre-paint reader must come before the stylesheets.
        html = board(tmp_path)
        head = html.split("</head>", 1)[0]
        assert head.index("dataset.gdFont") < head.index("tokens.css")


class TestVersion:
    def test_footer_shows_the_package_version(self, tmp_path):
        c = client(tmp_path)
        for page in ("/", "/alex/"):
            html = c.get(page).get_data(as_text=True)
            assert f"gerrit-dashboard {__version__}" in html

    def test_version_is_a_release_string(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)

    def test_version_comes_from_pyproject(self):
        # pyproject.toml is what the repo's pre-commit hook bumps; the
        # package must report that, not a copy that drifts.
        from pathlib import Path
        text = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        assert re.search(r'^version = "([^"]+)"$', text, re.M).group(1) == __version__


def get(tmp_path, page, design=None, **over):
    c = client(tmp_path, **over)
    if design:
        c.set_cookie("gd-design", design)
    return c.get(page).get_data(as_text=True)


class TestClassicDesign:
    @pytest.mark.parametrize("page", ["/", "/alex/"])
    def test_cookie_selects_the_classic_templates(self, tmp_path, page):
        tlc = get(tmp_path / "a", page)
        classic = get(tmp_path / "b", page, design="classic")
        assert "--panel2" in classic and "--panel2" not in tlc   # the classic palette
        assert 'var DESIGN = "classic";' in classic
        assert 'var DESIGN = "tlc";' in tlc

    @pytest.mark.parametrize("page", ["/", "/alex/"])
    def test_both_designs_share_one_display_menu(self, tmp_path, page):
        for design in (None, "classic"):
            html = get(tmp_path / (design or "tlc"), page, design=design)
            assert "data-tlc-display" in html and "tlc-display.js" in html
            assert 'var KEY = "gd-display";' in html
            assert 'id="gd-theme-sel"' not in html and 'id="gd-design-sel"' not in html

    def test_classic_has_one_font_and_no_contrast_or_gruvbox(self, tmp_path):
        html = get(tmp_path, "/alex/", design="classic")
        assert "var FONT_CHOICE = DESIGN === \"tlc\" && !BRANDED;" in html
        assert 'button[data-key="contrast"]' in html      # removed in classic
        assert "gruvbox" not in html

    def test_classic_follows_the_system_theme_on_auto(self, tmp_path):
        html = get(tmp_path, "/alex/", design="classic")
        assert "@media (prefers-color-scheme: light)" in html
        assert "html:not([data-theme])" in html

    def test_classic_scales_text_size_by_zoom(self, tmp_path):
        html = get(tmp_path, "/alex/", design="classic")
        assert 'html[data-gd-size="xs"] body { zoom: .875; }' in html
        assert 'html[data-text-size="l"] body { zoom: 1.125; }' in html

    def test_unknown_cookie_value_means_tlc(self, tmp_path):
        assert "tlc-display.js" in get(tmp_path, "/alex/", design="bogus")

    def test_tlc_panel_offers_the_classic_design(self, tmp_path):
        html = get(tmp_path, "/alex/")
        assert 'group("Design"' in html and "gd-design=classic" in html

    @pytest.mark.parametrize("page", ["/", "/alex/"])
    def test_classic_footer_shows_the_version(self, tmp_path, page):
        assert f"gerrit-dashboard {__version__}" in get(tmp_path, page, design="classic")

    def test_export_follows_the_chosen_design(self, tmp_path):
        c = client(tmp_path)
        c.set_cookie("gd-design", "classic")
        html = c.get("/alex/export").get_data(as_text=True)
        assert "--panel2" in html
        assert "var SWITCHABLE = false;" in html      # no switching in a static file
        assert "@font-face" not in html               # the menu's CSS ships without fonts

    def test_cli_export_is_tlc(self, tmp_path):
        # The CLI renders in a cookie-less test request: always TLC.
        from flask import render_template
        cfg = Config(); cfg.data_dir = tmp_path
        snap = build_snapshot(mk_bundle([(mk_change(), {"mine"})]), cfg)
        app = create_app(cfg, start_refresher=False)
        with app.test_request_context():
            html = render_template("dashboard.html", snapshot=snap, refreshing=False,
                                   error=None, form_error="", static_mode=True,
                                   gerrit_url="https://x", user="alex")
        assert "Atkinson" in html


class TestBrandingAcrossDesigns:
    @pytest.mark.parametrize("page", ["/", "/alex/"])
    def test_classic_unbranded_has_no_mark_and_system_font(self, tmp_path, page):
        html = get(tmp_path, page, design="classic")
        assert "gd-mark" not in html.split("</style>")[-1]   # no <img class="gd-mark">
        assert 'rel="icon"' not in html
        assert "@font-face" not in html

    @pytest.mark.parametrize("page", ["/", "/alex/"])
    def test_classic_branded_carries_mark_favicon_and_tlc_font(self, tmp_path, page):
        html = get(tmp_path, page, design="classic", tlc_branding=True)
        assert '<img class="gd-mark"' in html
        assert 'rel="icon"' in html
        assert "@font-face" in html and "tlc/fonts/atkinson-next" in html
        assert 'body { font-family: "Atkinson Hyperlegible Next"' in html

    def test_branded_tlc_design_forces_the_tlc_font(self, tmp_path):
        html = get(tmp_path, "/alex/", tlc_branding=True)
        assert "var BRANDED = true;" in html
        assert "&& false) document.documentElement.dataset.gdFont" in html

    def test_unbranded_tlc_design_offers_the_font_choice(self, tmp_path):
        html = get(tmp_path, "/alex/")
        assert "var BRANDED = false;" in html


class TestFontFaces:
    def test_live_urls_point_at_static(self, tmp_path):
        from gerrit_dashboard.app import tlc_font_faces
        cfg = Config(); cfg.data_dir = tmp_path
        app = create_app(cfg, start_refresher=False)
        with app.test_request_context():
            css = tlc_font_faces()
        assert css.count("@font-face") >= 4
        assert "../fonts/" not in css and "/static/tlc/fonts/" in css

    def test_inline_uses_data_uris(self, tmp_path):
        from gerrit_dashboard.app import tlc_font_faces
        css = tlc_font_faces(inline=True)
        assert "data:font/woff2;base64," in css and "/static/" not in css
