from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UiControlLayoutTests(unittest.TestCase):
    def test_sort_and_view_live_in_toolbar_dropdown(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        filters = html.split('<div class="filters" id="filters">', 1)[1].split(
            '<div class="scope-sticky" id="scopeSticky">', 1
        )[0]
        toolbar = html.split('<div class="scope-sticky" id="scopeSticky">', 1)[1].split(
            '<div class="status-bar" id="status">', 1
        )[0]

        self.assertNotIn('id="sortTags"', filters)
        self.assertNotIn('id="viewTags"', filters)
        self.assertIn('id="scopeFilterBtn"', toolbar)
        self.assertIn('id="scopeOptions"', toolbar)
        self.assertIn('id="sortTags"', toolbar)
        self.assertIn('id="viewTags"', toolbar)
        self.assertIn('id="scopeOptionsText"', toolbar)

    def test_thumbnail_images_use_non_blocking_retry_flow(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function thumbLoadFailed(img)", html)
        self.assertIn("function requestThumbReload(img, url)", html)
        self.assertIn("&defer=1", html)
        self.assertNotIn("dataset.retried", html)

    def test_feedback_and_probe_preferences_live_in_settings(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        header = html.split('<div class="header-actions">', 1)[1].split("</header>", 1)[0]
        settings = html.split('<div class="modal" id="privacyModal">', 1)[1].split(
            '<div class="modal" id="cleanupModal">', 1
        )[0]

        self.assertIn('id="settingsBtn"', header)
        self.assertNotIn('id="feedbackBtn"', header)
        self.assertIn('id="probeVideoDuration"', settings)
        self.assertIn('id="probeVideoAudio"', settings)
        self.assertIn("反馈问题", settings)


if __name__ == "__main__":
    unittest.main()
