import json
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from splitter import (Boundary, Cancelled, Cue, MB, Runner, Settings, SplitError,
                      adjusted_rate, choose_boundary, detect_scenes, encode_piece,
                      find_tool, parse_time, probe, read_srt, seconds_text,
                      split_movie, write_segment_srt)
from setup_tools import ensure_tools


class LogicTests(unittest.TestCase):
    def test_scene_window_nearest_and_subtitle(self):
        settings = Settings(window=10)
        cues = [Cue(97, 103, "대사")]
        picked = choose_boundary(100, [89, 98, 105, 112], cues, 0, 200, settings)
        self.assertEqual(picked.time, 105)
        self.assertEqual(picked.kind, "scene")
        settings.avoid_subtitle = False
        self.assertEqual(choose_boundary(100, [98, 105], cues, 0, 200, settings).time, 98)

    def test_no_scene_does_not_claim_scene(self):
        settings = Settings(window=10)
        b = choose_boundary(100, [], [Cue(98, 102, "대사")], 0, 200, settings)
        self.assertEqual(b.kind, "subtitle_end_fallback")
        self.assertLessEqual(abs(b.time - 100), 10)
        settings.allow_time_fallback = False
        with self.assertRaises(SplitError):
            choose_boundary(100, [], [], 0, 200, settings)

    def test_boundaries_not_reversed(self):
        b = choose_boundary(5, [-3, 1, 4.9, 7], [], 4.8, 20, Settings())
        self.assertGreater(b.time, 4.8)
        self.assertEqual(b.time, 7)

    def test_time_rounding_carry(self):
        self.assertEqual(seconds_text(59.9997), "00:01:00.000")
        self.assertEqual(parse_time("01:02:03,125"), 3723.125)

    def test_srt_offset_cp949_and_boundary_clipping(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "자막.srt"
            path.write_bytes("1\n00:00:03,000 --> 00:00:07,000\n곰줄 대사\n".encode("cp949"))
            cues, enc = read_srt(path, -1)
            self.assertEqual(enc, "cp949")
            self.assertEqual((cues[0].start, cues[0].end), (2, 6))
            a, b = Path(td)/"a.srt", Path(td)/"b.srt"
            write_segment_srt(a, cues, 0, 4)
            write_segment_srt(b, cues, 4, 8)
            self.assertIn("00:00:02,000 --> 00:00:04,000", a.read_text(encoding="utf-8-sig"))
            self.assertIn("00:00:00,000 --> 00:00:02,000", b.read_text(encoding="utf-8-sig"))

    def test_invalid_srt_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/"bad.srt"
            p.write_text("1\n00:00:07,000 --> 00:00:03,000\ntext\n")
            with self.assertRaises(SplitError):
                read_srt(p)

    def test_size_units_and_retry(self):
        self.assertEqual(Settings().max_bytes, 250_000_000)
        settings = Settings(max_bytes=512*MB)
        self.assertLess(adjusted_rate(2_000_000, 600*MB, settings, 192_000), 2_000_000)

    def test_pre_cancel(self):
        event = threading.Event(); event.set()
        with self.assertRaises(Cancelled):
            Runner(event).run([find_tool("ffprobe"), "-version"])

    def test_ready_tools_do_not_download(self):
        with patch("setup_tools.urllib.request.urlopen", side_effect=AssertionError("Unexpected download")):
            ensure_tools(Runner())


class VideoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="gomjul_tests_")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "원본 공백 & special.mp4"
        # 12 seconds, four known visual transitions, with continuous audio.
        ff = find_tool("ffmpeg")
        subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=red:s=320x180:r=24:d=3", "-f", "lavfi", "-i",
                        "color=blue:s=320x180:r=24:d=3", "-f", "lavfi", "-i",
                        "color=white:s=320x180:r=24:d=3", "-f", "lavfi", "-i",
                        "testsrc2=s=320x180:r=24:d=3", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=12",
                        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
                        "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "18", "-g", "48", "-c:a", "aac", "-b:a", "128k", str(cls.source)], check=True)
        cls.srt = cls.root / "원본 자막.srt"
        cls.srt.write_text("1\n00:00:02,000 --> 00:00:04,000\n경계에 걸친 대사\n\n2\n00:00:10,000 --> 00:00:11,500\n마지막 대사\n", encoding="utf-8-sig")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_scene_detection_has_source_times(self):
        info = probe(self.source, Runner())
        scenes = detect_scenes(self.source, 6, info["duration_seconds"], 2, .30, info["video"]["index"], Runner())
        self.assertTrue(any(abs(t - 6) < .1 for t in scenes), scenes)
        self.assertTrue(all(4 <= t <= 8 for t in scenes))

    def test_complete_split_size_audio_subtitles_coverage(self):
        events = []
        settings = Settings(max_bytes=300_000, window=2, preset="veryfast")
        folder = split_movie(self.source, self.root / "output", settings, self.srt, Runner(emit=events.append))
        m = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(m["status"], "completed")
        self.assertGreater(len(m["parts"]), 1)
        self.assertTrue(m["planned_source_coverage_verified"])
        self.assertEqual(m["parts"][0]["source_start_seconds"], 0)
        self.assertAlmostEqual(m["parts"][-1]["source_end_seconds"], m["source"]["duration_seconds"], places=5)
        for i, p in enumerate(m["parts"]):
            self.assertLessEqual(p["size_bytes"], settings.max_bytes)
            self.assertTrue(p["full_decode_verified"])
            self.assertTrue((folder / p["subtitle_file"]).is_file())
            info = probe(folder / p["file"], Runner())
            self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
            if i:
                self.assertEqual(m["parts"][i-1]["source_end_seconds"], p["source_start_seconds"])
        self.assertEqual((folder/"original_subtitles.srt").read_bytes(), self.srt.read_bytes())
        self.assertTrue((folder/"원본_시간표.csv").is_file())
        self.assertFalse(list(folder.glob("_processing_*")))

    def test_actual_oversize_is_reencoded_not_truncated(self):
        busy = self.root / "busy.mp4"
        subprocess.run([find_tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=s=640x360:r=24:d=6", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", str(busy)], check=True)
        info = probe(busy, Runner())
        with tempfile.TemporaryDirectory(dir=self.root) as td:
            out, meta, rate, retries = encode_piece(busy, 0, 6, 1_800_000, info["video"], None,
                                                    Settings(max_bytes=400_000, preset="veryfast"), Path(td), Runner())
            self.assertGreater(retries, 0)
            self.assertLessEqual(out.stat().st_size, 400_000)
            self.assertAlmostEqual(meta["duration_seconds"], 6, delta=.15)
            self.assertLess(rate, 1_800_000)

    def test_cancel_preserves_completed_parts_and_marks_incomplete(self):
        event = threading.Event()
        folders = []
        def emit(e):
            if e["type"] == "folder":
                folders.append(Path(e["path"]))
            if e["type"] == "part_done":
                event.set()
        with self.assertRaises(Cancelled):
            split_movie(self.source, self.root/"cancel", Settings(max_bytes=300_000, window=2, preset="veryfast"),
                        runner=Runner(event, emit))
        m = json.loads((folders[0]/"manifest.json").read_text())
        self.assertEqual(m["status"], "cancelled")
        self.assertEqual(len(m["parts"]), 1)
        self.assertTrue((folders[0]/m["parts"][0]["file"]).exists())

    def test_resume_reuses_valid_part_without_subtitles(self):
        event = threading.Event()
        folders = []
        settings = Settings(max_bytes=300_000, window=2, preset="veryfast")
        def emit(e):
            if e["type"] == "folder":
                folders.append(Path(e["path"]))
            if e["type"] == "part_done":
                event.set()
        output = self.root / "resume"
        with self.assertRaises(Cancelled):
            split_movie(self.source, output, settings, runner=Runner(event, emit))
        first = folders[0] / "Part_001.mp4"
        old_bytes, old_mtime = first.read_bytes(), first.stat().st_mtime_ns
        # Simulate a crash between promoting a file and committing its manifest.
        (folders[0] / "Part_002.mp4").write_bytes(b"uncommitted")
        folder = split_movie(self.source, output, settings)
        self.assertEqual(folder, folders[0])
        self.assertEqual(first.read_bytes(), old_bytes)
        self.assertEqual(first.stat().st_mtime_ns, old_mtime)
        self.assertEqual(len(list(folder.glob("미완료_보관_*/Part_002.mp4"))), 1)
        m = json.loads((folder/"manifest.json").read_text())
        self.assertEqual(m["status"], "completed")
        self.assertIsNone(m["subtitle"])
        self.assertFalse(list(folder.rglob("*.srt")))
        for p in m["parts"]:
            self.assertNotIn("subtitle_file", p)
            info = probe(folder/p["file"], Runner())
            self.assertFalse(any(s["codec_type"] == "subtitle" for s in info["streams"]))
        # A repeated completed job also reuses the validated videos.
        self.assertEqual(split_movie(self.source, output, settings), folder)
        self.assertEqual(first.stat().st_mtime_ns, old_mtime)
        with_subs = split_movie(self.source, output, settings, self.srt)
        self.assertNotEqual(with_subs, folder)
        self.assertTrue((with_subs / "original_subtitles.srt").exists())

    def test_resume_rejects_changed_output(self):
        settings = Settings(max_bytes=1_000_000, window=2, preset="veryfast")
        output = self.root / "corrupt"
        folder = split_movie(self.source, output, settings)
        part = folder / "Part_001.mp4"
        data = bytearray(part.read_bytes())
        data[-1] ^= 1
        part.write_bytes(data)
        with self.assertRaisesRegex(SplitError, "변경"):
            split_movie(self.source, output, settings)
        self.assertEqual(part.read_bytes(), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
