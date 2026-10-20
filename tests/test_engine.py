"""Offline unit checks for URL policy and format selection (no media is downloaded)."""
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from fastapi import HTTPException

import main


class URLPolicyTests(unittest.TestCase):
    def test_requires_https(self):
        with self.assertRaises(HTTPException) as caught:
            main.normalize_and_validate_url("http://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(caught.exception.status_code, 400)

    def test_rejects_localhost(self):
        with self.assertRaises(HTTPException):
            main.normalize_and_validate_url("https://localhost/private")

    def test_rejects_ip_literal(self):
        with self.assertRaises(HTTPException):
            main.normalize_and_validate_url("https://127.0.0.1/video")

    def test_rejects_arbitrary_generic_url(self):
        with self.assertRaises(HTTPException) as caught:
            main.normalize_and_validate_url("https://example.org/a-video")
        self.assertEqual(caught.exception.status_code, 422)

    def test_accepts_canonical_youtube_url(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with patch.object(main, "_ensure_public_dns", return_value=None):
            self.assertEqual(main.normalize_and_validate_url(url), url)

    def test_accepts_canonical_pinterest_pin_url(self):
        url = "https://www.pinterest.com/pin/123456789/"
        with patch.object(main, "_ensure_public_dns", return_value=None):
            self.assertEqual(main.normalize_and_validate_url(url), url)

    def test_rejects_host_that_resolves_to_private_address(self):
        answer = (None, None, None, None, ("10.0.0.8", 443))
        with patch.object(main, "_matching_extractor", return_value="Example"):
            with patch.object(main.socket, "getaddrinfo", return_value=[answer]):
                with self.assertRaises(HTTPException):
                    main.normalize_and_validate_url("https://videos.example/watch/123")

    def test_shortener_allowlist_is_narrow(self):
        self.assertTrue(main._is_pinterest_host("pin.it"))
        self.assertTrue(main._is_pinterest_host("www.pinterest.com"))
        self.assertTrue(main._is_pinterest_host("www.pinterest.co.uk"))
        self.assertFalse(main._is_pinterest_host("pinterest.com.evil.example"))
        self.assertFalse(main._is_pinterest_host("evil.example"))

    def test_pinterest_redirect_is_canonicalized_without_tracking_query(self):
        resolved = main._canonical_pinterest_pin(
            "https://www.pinterest.com/pin/some-title--123456789/sent/?invite_code=tracking"
        )
        self.assertEqual(resolved, "https://www.pinterest.com/pin/123456789/")

    def test_pinterest_shortener_blocks_non_pinterest_redirects(self):
        with patch.object(main, "_ensure_public_dns", return_value=None):
            with self.assertRaises(HTTPError):
                main._validate_pinterest_redirect("https://evil.example/redirect")

    def test_pinterest_short_url_resolves_without_following_an_untrusted_host(self):
        opener = MagicMock()
        response = MagicMock()
        response.__enter__.return_value.geturl.return_value = (
            "https://www.pinterest.com/pin/123456789/sent/?invite_code=track"
        )
        opener.open.return_value = response
        with patch.object(main, "_ensure_public_dns", return_value=None), patch.object(main, "build_opener", return_value=opener):
            resolved = main.resolve_pinterest_short_url("https://pin.it/abc123")
        self.assertEqual(resolved, "https://www.pinterest.com/pin/123456789/")

    def test_pinterest_short_url_flows_through_dedicated_extractor_validation(self):
        expected = "https://www.pinterest.com/pin/123456789/"
        with patch.object(main, "resolve_pinterest_short_url", return_value=expected), patch.object(main, "_ensure_public_dns", return_value=None):
            self.assertEqual(main.normalize_and_validate_url("https://pin.it/abc123"), expected)

    def test_rejects_url_credentials(self):
        with self.assertRaises(HTTPException):
            main.normalize_and_validate_url("https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_rejects_explicit_port(self):
        with self.assertRaises(HTTPException):
            main.normalize_and_validate_url("https://www.youtube.com:443/watch?v=dQw4w9WgXcQ")


class FormatSelectionTests(unittest.TestCase):
    def test_mp3_requires_audio_postprocessor(self):
        selector, processors = main.choose_format("mp3", "best")
        self.assertIn("bestaudio", selector)
        self.assertEqual(processors[0]["preferredcodec"], "mp3")

    def test_supported_audio_formats_use_expected_ffmpeg_codec(self):
        expected = {"m4a": "m4a", "aac": "aac", "flac": "flac", "opus": "opus", "ogg": "vorbis", "wav": "wav"}
        for extension, codec in expected.items():
            with self.subTest(extension=extension):
                selector, processors = main.choose_format(extension, "best")
                self.assertEqual(selector, "bestaudio/best")
                self.assertEqual(processors[0]["key"], "FFmpegExtractAudio")
                self.assertEqual(processors[0]["preferredcodec"], codec)

    def test_output_extension_table_covers_all_audio_formats(self):
        self.assertEqual(set(main.AUDIO_OUTPUT_EXTENSIONS), {"mp3", "m4a", "aac", "flac", "opus", "ogg", "wav"})
        self.assertEqual(main.AUDIO_OUTPUT_EXTENSIONS["ogg"], ".ogg")

    def test_audio_outputs_use_correct_content_types(self):
        expected = {
            ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
            ".flac": "audio/flac", ".opus": "audio/opus", ".ogg": "audio/ogg",
            ".wav": "audio/wav", ".mp4": "video/mp4", ".webm": "video/webm",
        }
        for extension, content_type in expected.items():
            with self.subTest(extension=extension):
                self.assertEqual(main.media_type_for_extension(extension), content_type)
        self.assertEqual(main.media_type_for_extension(".unknown"), "application/octet-stream")

    def test_720p_selector_is_bounded(self):
        selector, processors = main.choose_format("mp4", "720")
        self.assertIn("height<=720", selector)
        self.assertIsNone(processors)

    def test_best_mp4_prefers_mp4_muxed_formats(self):
        selector, processors = main.choose_format("mp4", "best")
        self.assertIn("ext=mp4", selector)
        self.assertIsNone(processors)


class FormatEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.formats = [
            {"format_id":"137", "ext":"mp4", "height":1080, "width":1920, "fps":30, "vcodec":"avc1", "acodec":"none", "filesize":12_000_000, "tbr":4500},
            {"format_id":"140", "ext":"m4a", "vcodec":"none", "acodec":"mp4a", "filesize_approx":2_000_000, "abr":128},
            {"format_id":"22", "ext":"mp4", "height":720, "width":1280, "fps":30, "vcodec":"avc1", "acodec":"mp4a", "filesize":8_000_000, "tbr":2500},
            {"format_id":"247", "ext":"webm", "height":1080, "width":1920, "fps":60, "vcodec":"vp9", "acodec":"none", "filesize_approx":16_000_000, "tbr":6000},
            {"format_id":"251", "ext":"webm", "vcodec":"none", "acodec":"opus", "filesize_approx":1_000_000, "abr":160},
            {"format_id":"bad;selector", "ext":"mp4", "height":1440, "width":2560, "vcodec":"avc1", "acodec":"none", "filesize":20_000_000, "tbr":8000},
        ]

    def test_lists_real_resolutions_and_containers(self):
        options = main._build_download_options(self.formats)
        pairs = {(row["height"], row["container"]) for row in options}
        self.assertIn((1080, "mp4"), pairs)
        self.assertIn((1080, "webm"), pairs)
        self.assertIn((720, "mp4"), pairs)
        self.assertNotIn((1440, "mp4"), pairs)  # Unsafe selector IDs are skipped.

    def test_video_and_audio_size_estimates_are_combined(self):
        options = main._build_download_options(self.formats)
        mp4_1080 = next(row for row in options if row["height"] == 1080 and row["container"] == "mp4")
        self.assertEqual(mp4_1080["filesize"], 14_000_000)
        self.assertEqual(mp4_1080["audio_format_id"], "140")
        self.assertTrue(mp4_1080["size_is_estimate"])

    def test_combined_format_needs_no_audio_id(self):
        options = main._build_download_options(self.formats)
        mp4_720 = next(row for row in options if row["height"] == 720 and row["container"] == "mp4")
        self.assertEqual(mp4_720["format_id"], "22")
        self.assertIsNone(mp4_720["audio_format_id"])

    def test_selected_format_ids_are_used_as_exact_pair(self):
        self.assertEqual(main.choose_format("mp4", "1080", "137", "140"), ("137+140", None))
        self.assertEqual(main.choose_format("webm", "1080", "247", "251"), ("247+251", None))

    def test_keeps_distinct_frame_rates_for_same_resolution_and_container(self):
        formats = self.formats + [
            {"format_id":"299", "ext":"mp4", "height":1080, "width":1920, "fps":60, "vcodec":"avc1.64002a", "acodec":"none", "filesize":18_000_000, "tbr":7000},
        ]
        options = main._build_download_options(formats)
        rows = [row for row in options if row["height"] == 1080 and row["container"] == "mp4"]
        self.assertEqual({row["fps"] for row in rows}, {30, 60})

    def test_unknown_video_size_is_not_reported_as_audio_only_size(self):
        formats = [
            {"format_id":"137", "ext":"mp4", "height":1080, "width":1920, "fps":30, "vcodec":"avc1", "acodec":"none", "filesize":None, "filesize_approx":None, "tbr":4500},
            {"format_id":"140", "ext":"m4a", "vcodec":"none", "acodec":"mp4a", "filesize":2_000_000, "abr":128},
        ]
        option = main._build_download_options(formats)[0]
        self.assertIsNone(option["filesize"])
        self.assertTrue(option["size_is_estimate"])



class DownloadLifecycleTests(unittest.TestCase):
    def test_directory_setup_failure_marks_job_error(self):
        job_id = "a" * 32
        original_job = {
            "id": job_id, "status": "queued", "progress": 0,
            "message": "queued", "updated_at": 0,
        }
        with patch.dict(main.jobs, {job_id: original_job}, clear=False):
            with patch.object(main.Path, "mkdir", side_effect=OSError("disk unavailable")):
                main.run_download(job_id, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mp4", "best")
            self.assertEqual(main.jobs[job_id]["status"], "error")
            self.assertGreater(main.jobs[job_id]["updated_at"], 0)
            main.jobs.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
