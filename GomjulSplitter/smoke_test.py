"""Verify the packaged executable on a Windows runner; no user media required."""
import json
import tempfile
import traceback
from pathlib import Path

def run(report):
    result = {"passed": False}
    try:
        from app import App
        from splitter import Runner, Settings, find_tool, split_movie
        window = App()
        window.update()
        assert window.limit.get().startswith("250")
        assert not window.subtitle.get()
        window.destroy()
        result["window_created"] = True
        with tempfile.TemporaryDirectory(prefix="gomjul_smoke_") as td:
            root = Path(td)
            source = root / "movie.mp4"
            runner = Runner()
            runner.run([find_tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=s=320x180:r=24:d=10", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=10", "-c:v", "libx264", "-preset", "veryfast",
                        "-c:a", "aac", str(source)])
            settings = Settings(max_bytes=300000, window=1, preset="veryfast")
            folder = split_movie(source, root / "out", settings)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["status"] == "completed"
            assert manifest["all_parts_under_limit"]
            assert manifest["planned_source_coverage_verified"]
            assert not list(folder.glob("*.srt"))
            result["no_subtitle_parts"] = len(manifest["parts"])
            srt = root / "selected.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nTest subtitle\n", encoding="utf-8")
            subfolder = split_movie(source, root / "out", settings, srt)
            assert (subfolder / "original_subtitles.srt").read_bytes() == srt.read_bytes()
            assert (subfolder / "Part_001.srt").exists()
            result["optional_subtitles"] = True
        result["passed"] = True
    except BaseException:
        result["error"] = traceback.format_exc()
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1
