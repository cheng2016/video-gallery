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
        self.assertIn("function resetThumbQueue(", html)
        self.assertIn("&defer=1", html)
        self.assertIn("THUMB_CONCURRENCY", html)
        self.assertIn("function watchThumbs(", html)
        self.assertIn("data-thumb-url", html)
        self.assertIn("refresh({ nav: false })", html)
        self.assertIn("resetThumbQueue()", html)
        self.assertIn("cancelListRequests()", html)
        self.assertIn("MAX_LOADED_VIDEOS", html)
        self.assertIn("AbortController", html)
        self.assertNotIn("dataset.retried", html)
        self.assertNotIn("const probe = new Image()", html)

    def test_multi_disk_all_videos_badge_uses_catalog_total(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function catalogTotal(", html)
        self.assertIn("const allBadge = catalogTotal(total);", html)
        self.assertIn("renderChannels(state.categories || [], catalogTotal());", html)
        self.assertNotIn("renderChannels(state.categories || [], state.totalCount || 0)", html)
        self.assertNotIn(
            "const allBadge = (total != null && total > 0) ? total : (grand || 0);",
            html,
        )

    def test_feedback_and_probe_preferences_live_in_settings(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        header = html.split('<div class="header-actions">', 1)[1].split("</header>", 1)[0]
        settings = html.split('<div class="modal settings-modal" id="privacyModal">', 1)[1].split(
            '<div class="modal" id="cleanupModal">', 1
        )[0]

        self.assertIn('id="settingsBtn"', header)
        self.assertNotIn('id="feedbackBtn"', header)
        self.assertIn('id="probeVideoDuration"', settings)
        self.assertIn('id="probeVideoAudio"', settings)
        self.assertIn("反馈问题", settings)
        self.assertIn("overflow: auto", html.split(".settings-panel .modal-info", 1)[1].split("}", 1)[0])
        self.assertIn("settings-panel", settings)
        self.assertIn('id="privacySave"', settings.split('class="modal-actions"', 1)[1])

    def test_channel_switch_abort_is_not_treated_as_page_failure(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        abort_fn = html.split("function isAbortError(", 1)[1].split(
            "function cancelListRequests(", 1
        )[0]
        self.assertIn("channel-switch", abort_fn)
        self.assertIn("controller.signal.aborted", html)
        self.assertIn("if (isAbortError(reason)) return;", html)

    def test_user_actions_and_playback_failures_are_reported_with_operation_ids(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/client-log"', html)
        self.assertIn('"X-VG-Operation-ID"', html)
        self.assertIn('clientLog("ui_click"', html)
        self.assertIn('clientLog("page_load_requested"', html)
        self.assertIn('clientLog("player_error"', html)
        self.assertIn('clientLog("hls_error"', html)
        self.assertIn("function beginPlaybackOperation(", html)
        self.assertIn("CLIENT_LOG_CORE_EVENTS", html)
        self.assertIn("CLIENT_LOG_LOOP_EVENTS", html)
        self.assertIn("clientLogQueue", html)
        self.assertIn("events.length === 1 ? events[0] : { events }", html)
        self.assertIn("!state._fullLogging", html)


if __name__ == "__main__":
    unittest.main()
