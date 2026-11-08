"""Anki-rendered HTML in native-mock and AnkiWeb hosts, not live-client proof."""
import functools
import html
import http.server
import json
import os
import re
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from anki.collection import Collection
from playwright.sync_api import expect, sync_playwright

from conftest import import_dist
from word_atlas_anki.constants import PROJECT_REPO, ROOT, UPSTREAM_REPO
from word_atlas_anki.manifest import load_manifest

pytestmark = pytest.mark.browser
VIEWPORTS = [(320, 480), (360, 640), (390, 844), (768, 1024), (800, 720), (960, 640), (1024, 640), (1280, 720), (1280, 800)]
RESULTS = ROOT / "test-results"
MOCK_AUDIO = """(() => {
  Object.defineProperty(HTMLMediaElement.prototype, 'paused', {configurable:true, get() {return this.__paused !== false;}});
  Object.defineProperty(HTMLMediaElement.prototype, 'ended', {configurable:true, get() {return false;}});
  HTMLMediaElement.prototype.play = function() {
    if (window.__rejectPlay) return Promise.reject(new Error('simulated playback failure'));
    this.__paused = false; this.dispatchEvent(new Event('play')); return Promise.resolve();
  };
  HTMLMediaElement.prototype.pause = function() {this.__paused = true; this.dispatchEvent(new Event('pause'));};
})();"""
AUDIO_EVENTS = """(() => {
  window.__mediaEvents = [];
  window.__audioKind = audio => {
    const pronunciation = audio.closest('.wa-pronunciation');
    if (pronunciation) return pronunciation.dataset.accent;
    return audio.closest('.wa-example') === document.querySelector('.wa-example') ? 'ex1' : 'ex2';
  };
  for (const type of ['play', 'ended', 'error']) {
    document.addEventListener(type, event => {
      if (event.target instanceof HTMLAudioElement) {
        window.__mediaEvents.push({type, kind: window.__audioKind(event.target),
          active: Array.from(document.querySelectorAll('audio')).filter(audio => !audio.paused).length});
      }
    }, true);
  }
})();"""
WEB_AUDIO = MOCK_AUDIO + AUDIO_EVENTS + """(() => {
  window.__attempts = [];
  window.__pending = [];
  Object.defineProperty(HTMLMediaElement.prototype, 'ended', {configurable: true, get() {return this.__ended === true;}});
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function() {
    window.__attempts.push(window.__audioKind(this));
    this.__ended = false;
    if (window.__deferPlay) return new Promise((resolve, reject) => window.__pending.push({reject}));
    if (window.__rejectPlay) return Promise.reject(new DOMException('autoplay blocked', 'NotAllowedError'));
    return play.call(this);
  };
})();"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@contextmanager
def served_cards(out, manifest, temporary, *, selected=None, port=0, host="native"):
    assert host in ("native", "web")
    with closing(Collection(str(temporary / "preview.anki2"))) as col:
        import_dist(col, out, manifest)
        media = Path(col.media.dir())
        by_guid = dict(col.db.all("SELECT guid,id FROM notes"))
        cards = dict(col.db.all("SELECT nid,id FROM cards"))
        names = list(manifest["notes"]) if selected is None else selected
        for ident in names:
            card = col.get_card(cards[by_guid[manifest["notes"][ident]["guid"]]])
            for side, markup in (("front", card.question()), ("back", card.answer())):
                if host == "web":
                    tags = {"q": card.question_av_tags(), "a": card.answer_av_tags()}

                    def web_audio(match):
                        filename = tags[match[1]][int(match[2])].filename
                        # AnkiWeb drops non-MP3 sound tags, including the stop WAV.
                        if not re.search(r"\.mp3", filename, re.IGNORECASE):
                            return ""
                        return f'<audio controls><source src="{html.escape(filename, quote=True)}" type="audio/mpeg"></audio>'

                    markup = re.sub(r"\[anki:play:([qa]):(\d+)\]", web_audio, markup)
                    body = '<body><div id="quiz"><div id="qa_box" class="card card1"><div id="qa">' + markup + '</div></div></div></body>'
                else:
                    # The GUI supplies replay controls; this bridge only records clicks.
                    markup = re.sub(r"\[anki:play:([qa]):(\d+)\]", lambda m:
                        f'<a class="replay-button soundLink" href="playsound:{m[1]}:{m[2]}" '
                        f'onclick="window.__nativeActions.push(\'{m[1]}:{m[2]}\'); return false;">'
                        '<svg viewBox="0 0 64 64"><path d="M24 16 48 32 24 48Z"/></svg></a>', markup)
                    body = '<body class="card"><script>window.__nativeActions=[];</script>' + markup + '</body>'
                document = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1">'
                            '</head>' + body + '</html>')
                (media / f"_preview_{ident}_{side}.html").write_text(document, encoding="utf-8")
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(QuietHandler, directory=str(media)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", names
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    executable = os.environ.get("WA_BROWSER_EXECUTABLE")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "browser-version.txt").write_text(browser.version + "\n", encoding="utf-8")
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def rendered(built, tmp_path_factory):
    out, manifest, _ = built
    with served_cards(out, manifest, tmp_path_factory.mktemp("rendered-cards")) as rendered:
        yield rendered[0]


@pytest.fixture(scope="module")
def web_rendered(built, tmp_path_factory):
    out, manifest, _ = built
    with served_cards(out, manifest, tmp_path_factory.mktemp("web-cards"), host="web") as rendered:
        yield rendered[0]


def open_card(page, base, ident, side, *, theme="light", os_theme="dark"):
    page.emulate_media(color_scheme=os_theme)
    page.goto(f"{base}/_preview_{ident}_{side}.html", wait_until="networkidle")
    if theme != "light":
        page.locator("body").evaluate("(body, theme) => body.classList.add(theme)", theme)
    page.evaluate("""async () => {
      await document.fonts.load('500 20px "WA Fraunces"');
      await document.fonts.load('italic 16px "WA Fraunces"');
      await document.fonts.ready;
    }""")


def assert_layout(page, *, theme, side):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert page.locator('[data-accent="uk"]').is_visible()
    assert page.locator('[data-accent="us"]').is_visible()
    uk = page.locator('[data-accent="uk"] .wa-ipa').bounding_box()
    us = page.locator('[data-accent="us"] .wa-ipa').bounding_box()
    assert abs(uk["y"] - us["y"]) < 1, (uk, us)
    assert uk["x"] < us["x"]
    background = page.locator(".wa-card").evaluate("e => getComputedStyle(e).backgroundColor")
    assert background == ("rgb(255, 253, 248)" if theme == "light" else "rgb(19, 26, 43)")
    sky = page.locator("body").evaluate("e => getComputedStyle(e).backgroundImage")
    assert sky == "none"
    assert page.evaluate("Array.from(document.fonts).every(font => font.status === 'loaded')")
    assert page.locator("audio[autoplay]").count() == 0
    if side == "front":
        assert page.locator(".wa-illustration, .wa-meanings, .wa-example").count() == 0
        assert page.locator(".wa-front-body .wa-eyebrow").count() == 0
        assert "Recall the meaning" not in page.locator(".wa-card").inner_text()
        assert "A word. A world." not in page.locator(".wa-card").inner_text()
        assert page.locator(".wa-front-footer .wa-level").is_visible()
        assert page.locator(".wa-front-footer .wa-chip").count() == 2
    else:
        assert page.locator(".wa-source, .wa-entry-header .wa-specimen, .wa-entry-header .wa-brand").count() == 0
        assert "Word Atlas contributors" not in page.locator(".wa-card").inner_text()
        for selector in (".wa-meanings", ".wa-example", ".wa-tip", ".wa-usage-note"):
            for section in page.locator(selector).all():
                assert section.is_visible()
        for image in page.locator("img").all():
            assert image.evaluate("e => e.complete && e.naturalWidth === 512 && e.naturalHeight === 512")


@pytest.mark.parametrize("width,height", VIEWPORTS)
@pytest.mark.parametrize("theme", ["light", "nightMode", "night_mode"])
@pytest.mark.parametrize("side", ["front", "back"])
def test_layouts_and_opposite_os_theme(browser, rendered, width, height, theme, side):
    page = browser.new_page(viewport={"width": width, "height": height})
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    try:
        for ident in ("alpha", "epsilon"):
            open_card(page, rendered, ident, side, theme=theme, os_theme="dark" if theme == "light" else "light")
            assert_layout(page, theme=theme, side=side)
            assert page.evaluate("window.__nativeActions") == []
            page.screenshot(path=str(RESULTS / f"{ident}-{side}-{width}x{height}-{theme}.png"), full_page=True)
        assert failures == []
    finally:
        page.close()


@pytest.mark.parametrize("width,height,scale,theme", [
    (320, 480, 1, "light"),
    (360, 780, 3, "light"),
    (360, 780, 3, "nightMode"),
    (360, 780, 3, "night_mode"),
    (520, 800, 1, "light"),
    (640, 360, 1, "light"),
    (759, 640, 1, "light"),
    (760, 640, 1, "light"),
    (768, 1024, 1, "light"),
    (960, 640, 1, "light"),
    (960, 660, 1, "light"),
    (960, 661, 1, "light"),
    (1199, 800, 1, "light"),
])
def test_mobile_illustration_geometry(browser, rendered, width, height, scale, theme):
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
    try:
        open_card(page, rendered, "alpha", "back", theme=theme)
        assert_layout(page, theme=theme, side="back")
        image = page.locator(".wa-illustration").bounding_box()
        art = page.locator(".wa-art").bounding_box()
        size = min(160, width * 0.4, height * 0.22)
        assert image["height"] == pytest.approx(size, abs=1)
        assert image["width"] == pytest.approx(image["height"], abs=1)
        assert image["x"] + image["width"] / 2 == pytest.approx(art["x"] + art["width"] / 2, abs=1)
        if width < 760:
            assert art["height"] == pytest.approx(image["height"] + 17, abs=1)
            body = page.locator(".wa-entry-body").bounding_box()
            assert body["y"] >= art["y"] + art["height"] - 1
        else:
            assert art["width"] == pytest.approx(size, abs=1)
            assert art["height"] == pytest.approx(size, abs=1)
            assert page.locator(".wa-art").evaluate("e => getComputedStyle(e).position") == "absolute"
            padding = page.locator(".wa-entry-body").evaluate("e => [getComputedStyle(e).paddingTop, getComputedStyle(e).paddingBottom]")
            assert padding == (["6px", "6px"] if height <= 660 else ["10px", "10px"])
            assert page.locator(".wa-art").evaluate("e => getComputedStyle(e).top") == ("10px" if height <= 660 else "14px")
            header = page.locator(".wa-entry-header").bounding_box()
            assert art["y"] + art["height"] <= header["y"] + header["height"] + 1
        open_card(page, rendered, "zeta", "back", theme=theme)
        assert page.locator(".wa-art, .wa-illustration").count() == 0
        assert page.locator(".wa-entry-header").evaluate("e => getComputedStyle(e).paddingRight") == "0px"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        page.close()


@pytest.mark.parametrize("width", [759, 1199])
@pytest.mark.parametrize("scale", [1.25, 1.5])
def test_fractional_viewport_has_no_illustration_fallback(browser, rendered, width, scale):
    scaled = browser.browser_type.launch(
        headless=True, executable_path=os.environ.get("WA_BROWSER_EXECUTABLE"),
        args=[f"--force-device-scale-factor={scale}", f"--window-size={width},900"],
    )
    try:
        # Integer emulated viewports round away the fractional-width query gap.
        page = scaled.new_page(no_viewport=True)
        open_card(page, rendered, "alpha", "back")
        viewport = page.evaluate("({width: visualViewport.width, height: visualViewport.height, scale: visualViewport.scale})")
        assert viewport["scale"] == 1
        assert width < viewport["width"] < width + 1, viewport
        assert_layout(page, theme="light", side="back")
        image = page.locator(".wa-illustration").bounding_box()
        size = min(160, viewport["width"] * 0.4, viewport["height"] * 0.22)
        assert image["width"] == pytest.approx(size, abs=1)
        assert image["height"] == pytest.approx(size, abs=1)
        position = page.locator(".wa-art").evaluate("e => getComputedStyle(e).position")
        assert position == ("static" if width < 760 else "absolute")
    finally:
        scaled.close()


@pytest.mark.parametrize("side", ["front", "back"])
@pytest.mark.parametrize("width", [320, 960, 1280])
def test_anki_button_styles_do_not_offset_ipa(browser, rendered, side, width):
    page = browser.new_page(viewport={"width": width, "height": 720})
    try:
        for ident in ("alpha", "beta", "gamma", "delta"):
            open_card(page, rendered, ident, side)
            page.add_style_tag(content=".card button { margin: 6px 10px; min-width: 80px; min-height: 52px; font-size: 24px; line-height: 2; }")
            assert_layout(page, theme="light", side=side)
            for button in page.locator(".wa-manual-button:visible").all():
                box = button.bounding_box()
                size = 36 if side == "back" and width >= 760 else 44
                assert box["width"] == box["height"] == size
    finally:
        page.close()


@pytest.mark.parametrize("width,height", [(800, 720), (960, 640), (1024, 640), (1280, 720)])
def test_ordinary_back_fits_desktop(browser, rendered, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    try:
        open_card(page, rendered, "alpha", "back")
        assert_layout(page, theme="light", side="back")
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        assert page.locator(".wa-definition").first.evaluate("e => parseFloat(getComputedStyle(e).fontSize)") >= 16
    finally:
        page.close()


def test_description_has_centered_repository_links(browser, built, collection):
    out, manifest, _ = built
    import_dist(collection, out, manifest)
    page = browser.new_page()
    try:
        page.set_content("<main>" + collection.decks.by_name("Word Atlas")["desc"] + "</main>")
        assert page.locator("main > div").evaluate("e => getComputedStyle(e).textAlign") == "center"
        assert page.locator("main a").all_text_contents() == ["Word Atlas", "Anki 适配"]
        assert page.locator("main a").evaluate_all("links => links.map(link => link.getAttribute('href'))") == [UPSTREAM_REPO, PROJECT_REPO]
    finally:
        page.close()


@pytest.mark.parametrize("width", [390, 960, 1199, 1200, 1280])
def test_chinese_headings_and_responsive_example_columns(browser, rendered, width):
    page = browser.new_page(viewport={"width": width, "height": 800})
    try:
        open_card(page, rendered, "alpha", "back")
        headings = {" ".join(text.split()) for text in page.locator("h2").all_text_contents()}
        assert headings == {"例句 日常", "例句 雅思语境", "常用搭配", "词族", "近义", "反义", "用法提醒"}
        assert page.locator(".wa-tip h2").count() == 0
        for section in page.locator(".wa-example").all():
            title = section.locator("h2").bounding_box()
            button = section.locator(".wa-native-audio, .wa-manual-button").bounding_box()
            assert 0 <= button["x"] - (title["x"] + title["width"]) <= 10
            assert abs((button["y"] + button["height"] / 2) - (title["y"] + title["height"] / 2)) < 1
        first = page.locator(".wa-example").nth(0).bounding_box()
        second = page.locator(".wa-example").nth(1).bounding_box()
        if 760 <= width < 1200:
            assert abs(first["y"] - second["y"]) < 1
            assert first["x"] < second["x"]
        else:
            assert abs(first["x"] - second["x"]) < 1
            assert second["y"] >= first["y"] + first["height"]
    finally:
        page.close()


def test_tip_spacing_preserves_markup_and_is_idempotent(browser, rendered):
    page = browser.new_page()
    try:
        open_card(page, rendered, "alpha", "back")
        tip = page.locator(".wa-tip p")
        tip.evaluate("e => { e.innerHTML = '<em>library</em>是图书馆，搭配bookshop；支持Anki2功能。已有 English 空格。'; }")
        script = (ROOT / "templates/card.js").read_text(encoding="utf-8")
        page.evaluate(script)
        expected = "library 是图书馆，搭配 bookshop；支持 Anki2 功能。已有 English 空格。"
        assert tip.inner_text() == expected
        assert tip.locator("em").inner_text() == "library"
        page.evaluate(script)
        assert tip.inner_text() == expected
        assert tip.locator("em").count() == 1
    finally:
        page.close()


@pytest.mark.parametrize("side", ["front", "back"])
def test_both_ipa_missing_sides_and_optional_recordings(browser, rendered, side):
    page = browser.new_page(viewport={"width": 360, "height": 640})
    try:
        open_card(page, rendered, "alpha", side)
        assert page.locator('[data-accent="uk"] .wa-ipa').inner_text() == page.locator('[data-accent="us"] .wa-ipa').inner_text()
        open_card(page, rendered, "beta", side)
        assert page.locator('[data-accent="uk"] .wa-ipa').inner_text() != page.locator('[data-accent="us"] .wa-ipa').inner_text()
        for ident, missing in (("gamma", "uk"), ("delta", "us")):
            open_card(page, rendered, ident, side)
            assert page.locator(f'[data-accent="{missing}"] .wa-ipa-missing').inner_text() == "IPA unavailable"
        assert page.locator(".wa-pronunciations .wa-manual-button, .wa-native-audio").count() == 0
        open_card(page, rendered, "zeta", side)
        assert page.locator(".wa-example, .wa-tip, .wa-usage-note, .wa-collocations, .wa-relations").count() == 0
        if side == "back":
            page.set_viewport_size({"width": 1280, "height": 800})
            assert page.locator(".wa-illustration").count() == 0
            assert page.locator(".wa-entry-body").bounding_box()["width"] > 900
    finally:
        page.close()


def test_manual_players_native_replay_and_cleanup(browser, rendered):
    page = browser.new_page()
    page.add_init_script(MOCK_AUDIO)
    try:
        open_card(page, rendered, "alpha", "back")
        buttons = page.locator(".wa-manual-button")
        assert buttons.count() == 2
        buttons.nth(0).click()
        assert page.locator("audio").evaluate_all("items => items.map(e => e.paused)") == [False, True]
        assert len(page.evaluate("window.__nativeActions")) == 1
        buttons.nth(1).click()
        assert page.locator("audio").evaluate_all("items => items.map(e => e.paused)") == [True, False]
        assert len(page.evaluate("window.__nativeActions")) == 2
        page.locator(".wa-native-audio .replay-button").first.click()
        assert page.locator("audio").evaluate_all("items => items.every(e => e.paused)")
        buttons.nth(0).click()
        page.keyboard.press("r")
        assert page.locator("audio").evaluate_all("items => items.every(e => e.paused)")
        script = (ROOT / "templates/card.js").read_text(encoding="utf-8")
        page.evaluate(script)
        count = len(page.evaluate("window.__nativeActions"))
        page.locator('[data-accent="us"] .wa-ipa-pill').press("Enter")
        assert len(page.evaluate("window.__nativeActions")) == count + 1
        page.evaluate("window.oldAudio = Array.from(document.querySelectorAll('audio')); document.querySelector('.wa-card').remove()")
        page.wait_for_function("!window.__wordAtlasCard")
        assert page.evaluate("window.oldAudio.every(e => e.paused)")
        open_card(page, rendered, "alpha", "front")
        page.locator(".wa-manual-button").click()
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        assert page.locator("audio").evaluate("e => e.paused")
        assert page.evaluate("!window.__wordAtlasCard")
    finally:
        page.close()


def test_unavailable_native_bridge_and_rejected_playback(browser, rendered):
    page = browser.new_page()
    page.add_init_script(MOCK_AUDIO)
    try:
        open_card(page, rendered, "alpha", "front")
        page.evaluate("document.querySelector('.wa-native-stop').remove()")
        page.locator(".wa-manual-button").click()
        assert page.locator("audio").is_visible()
        assert page.locator("audio").evaluate("e => e.controls && e.paused")
        assert "cannot be interrupted" in page.locator(".wa-audio-status").inner_text()
        open_card(page, rendered, "alpha", "front")
        page.evaluate("window.__rejectPlay = true")
        page.locator(".wa-manual-button").click()
        expect(page.locator(".wa-audio-status")).to_contain_text("Playback did not start")
        assert page.locator("audio").is_visible()
    finally:
        page.close()


@pytest.mark.parametrize("side", ["front", "back"])
@pytest.mark.parametrize("width,theme", [(320, "light"), (960, "nightMode")])
def test_ankiweb_host_layout_and_speaker_buttons(browser, web_rendered, side, width, theme):
    page = browser.new_page(viewport={"width": width, "height": 720})
    page.add_init_script(WEB_AUDIO)
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    try:
        open_card(page, web_rendered, "alpha", side, os_theme="dark" if theme == "light" else "light")
        page.locator("#qa_box").evaluate("(box, theme) => box.classList.add(theme)", theme)
        page.add_style_tag(content=".card button { margin: 6px 10px; min-width: 80px; min-height: 52px; }")
        assert page.locator("#quiz > #qa_box.card.card1 > #qa > .wa-card").count() == 1
        assert page.locator("body.card, .replay-button, .soundLink").count() == 0
        assert page.evaluate("typeof window.__nativeActions") == "undefined"
        assert page.locator(".wa-native-stop").inner_html().strip() == ""
        assert_layout(page, theme=theme, side=side)
        assert page.locator("audio:visible, .wa-native-audio:visible").count() == 0
        assert page.locator("audio").evaluate_all("items => items.every(audio => !audio.controls && !audio.autoplay)")
        buttons = page.locator(".wa-manual-button")
        assert buttons.count() == (2 if side == "front" else 4)
        speaker = page.locator('[data-accent="us"] .wa-manual-button svg').evaluate("svg => svg.outerHTML")
        for button in buttons.all():
            assert button.is_visible() and button.is_enabled()
            assert button.get_attribute("aria-label")
            assert button.locator("svg").evaluate("svg => svg.outerHTML") == speaker
            box = button.bounding_box()
            assert box["width"] == box["height"] == (36 if side == "back" and width >= 760 else 44)
        for wrapper in page.locator(".wa-native-audio").all():
            assert wrapper.evaluate("e => e.parentElement.querySelectorAll(':scope > .wa-manual-button').length") == 1
        for group in page.locator(".wa-pronunciation").all():
            pill, button = group.locator(".wa-ipa-pill").bounding_box(), group.locator(".wa-manual-button").bounding_box()
            if width < 360:
                assert button["y"] >= pill["y"] + pill["height"]
            else:
                assert abs(button["y"] - pill["y"]) < 1
                assert button["x"] < pill["x"]
        expect(page.locator(".wa-audio-status")).to_be_hidden()
        assert failures == []
    finally:
        page.close()


@pytest.mark.parametrize("side,missing,expected", [
    ("front", [], ["uk"]),
    ("front", ["UK pronunciation"], []),
    ("back", [], ["uk", "ex1"]),
    ("back", ["UK pronunciation"], ["ex1"]),
    ("back", ["First example"], ["uk"]),
    ("back", ["UK pronunciation", "First example"], []),
])
def test_ankiweb_autoplay_order_and_missing_recordings(browser, web_rendered, side, missing, expected):
    page = browser.new_page()
    page.add_init_script(WEB_AUDIO)

    def omit_recordings(route):
        response = route.fetch()
        markup = response.text()
        for label in missing:
            markup = re.sub(r'<span class="wa-native-audio" data-audio-label="' + re.escape(label) + r'">.*?</span>', "", markup)
        route.fulfill(response=response, body=markup)

    page.route("**/_preview_alpha_*.html", omit_recordings)
    try:
        open_card(page, web_rendered, "alpha", side)
        assert page.evaluate("window.__attempts") == expected[:1]
        for index, kind in enumerate(expected):
            assert page.locator("audio").evaluate_all("items => items.filter(audio => !audio.paused).map(window.__audioKind)") == [kind]
            page.locator("audio").evaluate_all("items => { const audio = items.find(audio => !audio.paused); audio.__paused = audio.__ended = true; audio.dispatchEvent(new Event('ended')); }")
            assert page.evaluate("window.__attempts") == expected[:index + 2]
        assert page.locator("audio").evaluate_all("items => items.every(audio => audio.paused)")
        expect(page.locator(".wa-audio-status")).to_be_hidden()
        assert page.locator("audio:visible").count() == 0
    finally:
        page.close()


@pytest.mark.parametrize("manual", ["us", "ex2"])
def test_ankiweb_manual_interruption_and_r_replay(browser, web_rendered, manual):
    page = browser.new_page()
    page.add_init_script(WEB_AUDIO)
    try:
        open_card(page, web_rendered, "alpha", "back")
        button = (page.locator('[data-accent="us"] .wa-manual-button') if manual == "us" else
                  page.locator('.wa-example').last.locator(".wa-manual-button"))
        button.click()
        assert page.evaluate("window.__attempts") == ["uk", manual]
        assert page.locator("audio").evaluate_all("items => items.filter(audio => !audio.paused).map(window.__audioKind)") == [manual]
        expect(button).to_have_attribute("aria-pressed", "true")
        page.locator('[data-accent="uk"] audio').dispatch_event("ended")
        assert page.evaluate("window.__attempts") == ["uk", manual]
        button.click()
        assert page.locator("audio").evaluate_all("items => items.every(audio => audio.paused)")
        page.locator("audio").evaluate_all("items => items.forEach(audio => audio.dispatchEvent(new Event('ended')))")
        assert page.evaluate("window.__attempts") == ["uk", manual]
        page.keyboard.press("r")
        assert page.evaluate("window.__attempts") == ["uk", manual, "uk"]
        page.locator('[data-accent="uk"] audio').evaluate("audio => { audio.__paused = audio.__ended = true; audio.dispatchEvent(new Event('ended')); }")
        assert page.evaluate("window.__attempts") == ["uk", manual, "uk", "ex1"]
        assert page.locator("audio").evaluate_all("items => items.filter(audio => !audio.paused).map(window.__audioKind)") == ["ex1"]
        page.locator('[data-accent="us"] .wa-ipa-pill').press("Enter")
        assert page.evaluate("window.__attempts") == ["uk", manual, "uk", "ex1", "us"]
        assert page.locator("audio").evaluate_all("items => items.filter(audio => !audio.paused).map(window.__audioKind)") == ["us"]
    finally:
        page.close()


def test_ankiweb_rejected_autoplay_keeps_buttons_and_allows_retry(browser, web_rendered):
    page = browser.new_page()
    page.add_init_script(WEB_AUDIO + "window.__rejectPlay = true;")
    try:
        open_card(page, web_rendered, "alpha", "back")
        assert page.evaluate("window.__attempts") == ["uk"]
        status = page.locator(".wa-audio-status")
        expect(status).to_be_hidden()
        expect(status).to_have_text("")
        assert page.locator("audio:visible, audio[controls]").count() == 0
        assert page.locator(".wa-manual-button:visible").count() == 4
        # Explicit playback failures still report an error; only autoplay is silent.
        page.locator('[data-accent="us"] .wa-manual-button').click()
        expect(status).to_be_visible()
        expect(status).to_contain_text(re.compile(r"speaker|button|retry", re.IGNORECASE))
        page.evaluate("window.__rejectPlay = false")
        button = page.locator('[data-accent="uk"] .wa-manual-button')
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(status).to_be_hidden()
        page.locator('[data-accent="uk"] audio').evaluate("audio => { audio.__paused = audio.__ended = true; audio.dispatchEvent(new Event('ended')); }")
        assert page.evaluate("window.__attempts") == ["uk", "us", "uk"]
        page.locator(".wa-example").last.locator(".wa-manual-button").click()
        assert page.evaluate("window.__attempts") == ["uk", "us", "uk", "ex2"]
    finally:
        page.close()


@pytest.mark.parametrize("retry", ["uk", "us"])
def test_ankiweb_old_play_rejection_does_not_overwrite_new_state(browser, web_rendered, retry):
    page = browser.new_page()
    page.add_init_script(WEB_AUDIO + "window.__deferPlay = true;")
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    try:
        open_card(page, web_rendered, "alpha", "back")
        assert page.evaluate("window.__pending.length") == 1
        page.evaluate("window.__deferPlay = false")
        button = page.locator(f'[data-accent="{retry}"] .wa-manual-button')
        button.click()
        page.evaluate("window.__pending[0].reject(new DOMException('old attempt', 'NotAllowedError'))")
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".wa-audio-status")).to_be_hidden()
        assert page.locator("audio").evaluate_all("items => items.filter(audio => !audio.paused).map(window.__audioKind)") == [retry]
        assert page.evaluate("window.__attempts") == ["uk", retry]
        assert page.locator("audio:visible, audio[controls]").count() == 0
        assert failures == []
    finally:
        page.close()


@pytest.mark.parametrize("event", ["visibilitychange", "pagehide", "bfcache", "replacement"])
def test_ankiweb_reinitialization_and_lifecycle_cancel_queue(browser, web_rendered, event):
    page = browser.new_page()
    page.add_init_script(WEB_AUDIO)
    try:
        open_card(page, web_rendered, "alpha", "back")
        script = (ROOT / "templates/card.js").read_text(encoding="utf-8")
        page.evaluate(script)
        page.evaluate(script)
        assert page.locator(".wa-manual-button:visible").count() == 4
        assert page.evaluate("window.__attempts") == ["uk", "uk", "uk"]
        page.locator('[data-accent="us"] .wa-ipa-pill').press("Space")
        assert page.evaluate("window.__attempts") == ["uk", "uk", "uk", "us"]
        page.keyboard.press("r")
        assert page.evaluate("window.__attempts") == ["uk", "uk", "uk", "us", "uk"]
        page.evaluate("window.oldAudio = Array.from(document.querySelectorAll('audio'))")
        if event == "visibilitychange":
            page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, value: true}); document.dispatchEvent(new Event('visibilitychange'))")
        elif event == "pagehide":
            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        elif event == "bfcache":
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
        else:
            page.locator("#qa").evaluate("qa => qa.replaceChildren()")
        assert page.evaluate("window.oldAudio.every(audio => audio.paused)")
        if event in ("pagehide", "replacement"):
            page.wait_for_function("!window.__wordAtlasCard")
        page.evaluate("window.oldAudio.forEach(audio => audio.dispatchEvent(new Event('ended')))")
        assert page.evaluate("window.__attempts") == ["uk", "uk", "uk", "us", "uk"]
        if event in ("visibilitychange", "bfcache"):
            if event == "visibilitychange":
                page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, value: false}); document.dispatchEvent(new Event('visibilitychange'))")
            else:
                page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
            assert page.evaluate("window.__attempts") == ["uk", "uk", "uk", "us", "uk"]
            assert page.locator(".wa-manual-button:visible").count() == 4
            assert page.locator("audio:visible, audio[controls]").count() == 0
            page.locator('[data-accent="uk"] .wa-manual-button').click()
        elif event == "replacement":
            # AnkiWeb replaces #qa and then recreates its scripts on each card.
            page.evaluate("""async url => {
              const doc = new DOMParser().parseFromString(await (await fetch(url)).text(), 'text/html');
              const qa = document.querySelector('#qa');
              qa.innerHTML = doc.querySelector('#qa').innerHTML;
              qa.querySelectorAll('script').forEach(old => {
                const script = document.createElement('script'); script.textContent = old.textContent; old.replaceWith(script);
              });
            }""", f"{web_rendered}/_preview_alpha_front.html")
            assert page.locator(".wa-manual-button:visible").count() == 2
            page.evaluate("window.oldAudio.forEach(audio => audio.dispatchEvent(new Event('ended')))")
        else:
            return
        assert page.evaluate("window.__attempts") == ["uk", "uk", "uk", "us", "uk", "uk"]
        assert page.locator("audio").evaluate_all("items => items.filter(audio => !audio.paused).map(window.__audioKind)") == ["uk"]
    finally:
        page.close()


def test_ankiweb_html_only_fallback_has_real_audio_without_bridge(browser, web_rendered):
    page = browser.new_page(java_script_enabled=False)
    try:
        page.goto(f"{web_rendered}/_preview_alpha_back.html", wait_until="networkidle")
        assert page.locator("audio[controls]:visible").count() == 4
        assert page.locator(".wa-manual-button:visible, .replay-button, .soundLink, audio[autoplay]").count() == 0
        assert page.locator(".wa-native-stop").inner_html().strip() == ""
        sources = page.locator('.wa-native-audio audio > source[type="audio/mpeg"]')
        assert sources.count() == 2
        for source in sources.all():
            assert source.get_attribute("src").endswith(".mp3")
    finally:
        page.close()


@pytest.mark.parametrize("initially_missing", [False, True])
def test_ankiweb_real_mp3_autoplay_queue(browser, web_rendered, initially_missing):
    # Permit autoplay explicitly; policy rejection is covered separately above.
    media_browser = browser.browser_type.launch(
        headless=True, executable_path=os.environ.get("WA_BROWSER_EXECUTABLE"),
        args=["--autoplay-policy=no-user-gesture-required"],
    )
    page = media_browser.new_page()
    page.add_init_script(AUDIO_EVENTS)
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    if initially_missing:
        page.route("**/*.mp3", lambda route: route.fulfill(status=404, body="Not synced yet"))
    try:
        open_card(page, web_rendered, "alpha", "back")
        if initially_missing:
            status = page.locator(".wa-audio-status")
            expect(status).to_be_visible()
            expect(status).to_contain_text(re.compile(r"sync|retry", re.IGNORECASE))
            assert page.locator(".wa-manual-button:visible").count() == 4
            assert page.locator("audio:visible, audio[controls]").count() == 0
            assert page.locator("audio").evaluate_all("items => items.every(audio => audio.paused)")
            page.unroute("**/*.mp3")
            page.evaluate("window.__mediaEvents = []")
            page.locator('[data-accent="uk"] .wa-manual-button').click()
            page.wait_for_function("window.__mediaEvents.some(event => event.type === 'ended' && event.kind === 'uk')")
            assert page.evaluate("window.__mediaEvents.map(event => [event.type, event.kind])") == [["play", "uk"], ["ended", "uk"]]
            expect(status).to_be_hidden()
            page.evaluate("window.__mediaEvents = []")
            page.keyboard.press("r")
        page.wait_for_function("window.__mediaEvents.some(event => event.type === 'ended' && event.kind === 'ex1')")
        events = page.evaluate("window.__mediaEvents")
        assert [(event["type"], event["kind"]) for event in events] == [
            ("play", "uk"), ("ended", "uk"), ("play", "ex1"), ("ended", "ex1"),
        ]
        assert all(event["active"] <= 1 for event in events)
        assert page.locator("audio").evaluate_all("items => items.every(audio => audio.paused)")
        assert page.locator(".wa-native-audio audio").evaluate_all("items => items.every(audio => Number.isFinite(audio.duration) && audio.duration > 0)")
        assert page.locator("audio:visible, .wa-manual-button[aria-pressed='true']").count() == 0
        expect(page.locator(".wa-audio-status")).to_be_hidden()
        assert failures == []
    finally:
        media_browser.close()


@pytest.mark.parametrize("width", [320, 360, 390, 480])
def test_speaker_stacks_only_on_very_narrow_screens(browser, rendered, width):
    page = browser.new_page(viewport={"width": width, "height": 720})
    try:
        open_card(page, rendered, "alpha", "front")
        for accent in ("uk", "us"):
            group = page.locator(f'[data-accent="{accent}"]')
            pill = group.locator(".wa-ipa-pill").bounding_box()
            button = group.locator(".wa-native-audio, .wa-manual-button").bounding_box()
            if width < 360:
                assert button["y"] >= pill["y"] + pill["height"]
            else:
                assert abs(button["y"] - pill["y"]) < 1
                assert button["x"] < pill["x"]
    finally:
        page.close()


def test_native_press_feedback_does_not_fake_playing_state(browser, rendered):
    page = browser.new_page()
    try:
        open_card(page, rendered, "alpha", "front")
        native = page.locator(".wa-native-audio").first
        idle = native.evaluate("e => getComputedStyle(e).backgroundColor")
        box = native.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        assert native.evaluate("e => getComputedStyle(e).backgroundColor") != idle
        page.mouse.up()
        assert native.evaluate("e => getComputedStyle(e).backgroundColor") == idle
        assert not native.evaluate("e => e.classList.contains('is-playing')")
        assert len(page.evaluate("window.__nativeActions")) == 1
    finally:
        page.close()


@pytest.mark.parametrize("side", ["front", "back"])
@pytest.mark.parametrize("theme", ["light", "nightMode", "night_mode"])
def test_level_badges_and_speakers_share_grade_colors(browser, rendered, side, theme):
    palette = (["rgb(75, 107, 69)", "rgb(55, 107, 135)", "rgb(168, 67, 42)"] if theme == "light" else
               ["rgb(181, 209, 172)", "rgb(175, 208, 231)", "rgb(242, 154, 123)"])
    page = browser.new_page()
    page.add_init_script(MOCK_AUDIO)
    try:
        for i, ident in enumerate(("alpha", "beta", "gamma", "delta", "epsilon", "zeta")):
            open_card(page, rendered, ident, side, theme=theme)
            expected = palette[i // 2]
            badge = page.locator(".wa-level")
            assert page.locator(".wa-card").get_attribute("data-cefr") == badge.inner_text()
            assert badge.evaluate("e => getComputedStyle(e).color") == expected
            for control in page.locator(".wa-native-audio, .wa-manual-button").all():
                assert control.evaluate("e => getComputedStyle(e).color") == expected
                assert control.evaluate("e => getComputedStyle(e).backgroundColor") == badge.evaluate("e => getComputedStyle(e).backgroundColor")
            for control in page.locator(".wa-native-audio").all():
                assert control.evaluate("e => getComputedStyle(e, '::after').backgroundColor") == expected
            buttons = page.locator(".wa-manual-button")
            if buttons.count():
                buttons.first.click()
                assert buttons.first.evaluate("e => getComputedStyle(e).backgroundColor") == expected
                assert buttons.first.get_attribute("aria-pressed") == "true"
    finally:
        page.close()


@pytest.mark.parametrize("theme", ["nightMode", "night_mode"])
def test_dark_everyday_badge_has_visible_solid_background(browser, rendered, theme):
    page = browser.new_page()
    try:
        open_card(page, rendered, "alpha", "back", theme=theme, os_theme="light")
        badge = page.locator(".wa-example-tag:not(.wa-example-tag--extended)")
        assert badge.inner_text() == "日常"
        assert badge.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(36, 56, 76)"
        assert badge.evaluate("e => getComputedStyle(e).borderTopWidth") == "1px"
        assert badge.evaluate("e => getComputedStyle(e).borderTopStyle") == "solid"
        assert_layout(page, theme=theme, side="back")
    finally:
        page.close()


@pytest.mark.parametrize("theme", ["light", "nightMode"])
@pytest.mark.parametrize("ident", ["alpha", "gamma", "epsilon"])
def test_text_contrast(browser, rendered, theme, ident):
    page = browser.new_page()
    try:
        open_card(page, rendered, ident, "back", theme=theme)
        colors = page.locator(".wa-meaning,.wa-definition,.wa-pos,.wa-eyebrow,.wa-translation,.wa-ipa,.wa-example-tag,.wa-level,.wa-tip p").evaluate_all("""items => items.map(e => {
          const style = getComputedStyle(e), layers = []; let parent = e;
          while (parent) {layers.unshift(getComputedStyle(parent).backgroundColor); parent = parent.parentElement;}
          let background = [255, 255, 255];
          for (const layer of layers) {
            const values = (layer.match(/[\\d.]+/g) || []).map(Number);
            if (values.length < 3) continue;
            const alpha = values.length === 4 ? values[3] : 1;
            background = background.map((channel, i) => values[i] * alpha + channel * (1 - alpha));
          }
          return {name:e.className || e.tagName, color:style.color, background:'rgb(' + background.join(',') + ')'};
        })""")

        def luminance(color):
            rgb = [float(value) / 255 for value in re.findall(r"[\d.]+", color)[:3]]
            linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in rgb]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        for item in colors:
            foreground, background = luminance(item["color"]), luminance(item["background"])
            ratio = (max(foreground, background) + .05) / (min(foreground, background) + .05)
            assert ratio >= 4.5, (item, ratio)
    finally:
        page.close()


@pytest.mark.parametrize("side", ["front", "back"])
@pytest.mark.parametrize("theme", ["light", "nightMode", "night_mode"])
def test_english_typography_uses_sans_serif(browser, rendered, side, theme):
    page = browser.new_page(viewport={"width": 960, "height": 720})
    try:
        open_card(page, rendered, "alpha", side, theme=theme)
        families = page.locator(".wa-brand,.wa-headword,.wa-example-en,.wa-definition,.wa-pos,.wa-ipa").evaluate_all(
            "items => items.map(e => getComputedStyle(e).fontFamily)"
        )
        for family in families:
            assert family.endswith("sans-serif"), family
            assert not any(name in family for name in ("Fraunces", "Georgia", "Times New Roman", "Charis SIL", "Doulos SIL")), family
    finally:
        page.close()


@pytest.mark.skipif(not os.environ.get("WA_REAL_DIST"), reason="Real release deck is tested after the full build")
def test_real_deck_layouts(browser, tmp_path):
    out = Path(os.environ["WA_REAL_DIST"]).resolve()
    manifest = load_manifest(out / "manifest.json")
    selected = []
    for level in ("A1", "A2", "B1", "B2", "C1", "C2"):
        candidates = [ident for ident, note in manifest["notes"].items() if note["cefr"] == level]
        if candidates:
            selected.append(min(candidates, key=lambda ident: manifest["notes"][ident]["order"]))
    selected.append(max(manifest["notes"], key=lambda ident: len(manifest["notes"][ident]["word"])))
    with served_cards(out, manifest, tmp_path, selected=list(dict.fromkeys(selected))) as (base, names):
        page = browser.new_page()
        measurements = []
        try:
            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                for theme in ("light", "night_mode"):
                    for ident in names:
                        for side in ("front", "back"):
                            open_card(page, base, ident, side, theme=theme, os_theme="dark" if theme == "light" else "light")
                            assert_layout(page, theme=theme, side=side)
                            page.screenshot(path=str(RESULTS / f"real-{ident}-{side}-{width}x{height}-{theme}.png"), full_page=True)
                            if side == "back":
                                measurements.append({"id": ident, "viewport": [width, height], "theme": theme,
                                                     "card_height": page.locator(".wa-card").bounding_box()["height"],
                                                     "page_height": page.evaluate("document.documentElement.scrollHeight")})
                                if width >= 960 and ident in {"library", "cloud", "hut", "motivate", "cohesive", "avenge"}:
                                    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight"), measurements[-1]
            (RESULTS / "real-layouts.json").write_text(json.dumps(measurements, indent=2) + "\n", encoding="utf-8")
        finally:
            page.close()
