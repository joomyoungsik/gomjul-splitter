"""Fetch Windows FFmpeg from a provider linked by ffmpeg.org. Never uploads media."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def ensure_tools(runner):
    from splitter import find_tool, tools_dir
    try:
        for name in ("ffmpeg", "ffprobe"):
            runner.run([find_tool(name), "-version"])
        return
    except Exception:
        runner.check()
        if os.name != "nt":
            raise RuntimeError("FFmpeg와 ffprobe를 설치하세요.")
    install_tools(tools_dir(), runner)


def install_tools(target, runner):
    runner.check()
    runner.emit({"type": "stage", "message": "최초 영상 도구 자동 다운로드 · 영화는 전송하지 않습니다"})
    with urllib.request.urlopen(URL + ".sha256", timeout=30) as response:
        expected_text = response.read(4096).decode("utf-8")
    match = re.search(r"\b[0-9a-fA-F]{64}\b", expected_text)
    if not match:
        raise RuntimeError("No valid SHA256 checksum was provided. Download was not installed.")
    expected = match.group(0).lower()
    with tempfile.TemporaryDirectory(prefix="gomjul_ffmpeg_") as td:
        archive = Path(td) / "ffmpeg.zip"
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(URL, timeout=30) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                runner.check()
                size += len(chunk)
                if size > 500_000_000:
                    raise RuntimeError("Download unexpectedly exceeds 500MB. Stopped.")
                output.write(chunk)
                digest.update(chunk)
                runner.emit({"type": "stage", "message": f"영상 도구 다운로드 · {size/1_000_000:.1f} MB"})
        if digest.hexdigest() != expected:
            raise RuntimeError("SHA256 mismatch. No tools were installed. Retry in case the release changed.")
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                matches = [n for n in z.namelist() if n.endswith("/bin/" + name)]
                if len(matches) != 1:
                    raise RuntimeError("Unexpected archive layout")
                entry = z.getinfo(matches[0])
                if entry.file_size > 400_000_000:
                    raise RuntimeError("Unexpected tool size")
                temporary = target / (name + ".download")
                with z.open(entry) as src, temporary.open("wb") as dest:
                    shutil.copyfileobj(src, dest)
                os.replace(temporary, target / name)
            # Preserve provider notices alongside binaries. Never extract arbitrary paths.
            for name in ("LICENSE", "LICENSE.txt", "README.txt"):
                matches = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == name]
                if matches:
                    (target / name).write_bytes(z.read(matches[0]))
        (target / "download_receipt.json").write_text(json.dumps({"source": URL, "sha256": expected}, indent=2), encoding="utf-8")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        runner.run([str(target / name), "-version"])
    runner.emit({"type": "log", "message": "영상 도구 준비 완료"})


def main():
    from splitter import Runner
    ensure_tools(Runner(emit=lambda event: print(event.get("message", ""))))


if __name__ == "__main__":
    main()
