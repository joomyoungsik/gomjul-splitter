"""Gomjul Splitter: local scene-aware, size-verified analysis copies.

Python 3.10+; FFmpeg/ffprobe 6+. No third-party Python runtime dependency.
All external processes use argument arrays, never shell=True.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

MB = 1_000_000  # decimal MB: conservative even when an uploader uses MiB


class SplitError(Exception):
    pass


class Cancelled(SplitError):
    pass


@dataclass
class Settings:
    max_bytes: int = 250 * MB
    margin: float = 0.08
    window: float = 10.0
    scene_threshold: float = 0.30
    avoid_subtitle: bool = True
    allow_time_fallback: bool = True
    subtitle_offset: float = 0.0  # video seconds = SRT seconds + offset
    subtitle_encoding: str = "auto"
    max_height: int = 0
    audio_index: int | None = None  # absolute stream index; None = first audio
    full_verify: bool = True
    preset: str = "fast"

    def validate(self):
        if not 250_000 <= self.max_bytes <= 512 * MB:
            raise SplitError("파일 상한은 0.25~512MB 범위여야 합니다.")
        if not 0.03 <= self.margin <= 0.25:
            raise SplitError("용량 여유는 3~25% 범위여야 합니다.")
        if not math.isfinite(self.window) or not 0 <= self.window <= 60:
            raise SplitError("장면 검색 범위는 0~60초여야 합니다.")
        if not 0.01 <= self.scene_threshold <= 0.95:
            raise SplitError("장면 감지 기준은 0.01~0.95여야 합니다.")
        if not math.isfinite(self.subtitle_offset):
            raise SplitError("자막 보정값이 올바르지 않습니다.")
        if self.max_height not in (0, 720, 1080):
            raise SplitError("해상도는 원본, 1080p 또는 720p를 선택하세요.")
        if self.preset not in ("veryfast", "fast", "medium"):
            raise SplitError("잘못된 인코딩 설정입니다.")


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Boundary:
    time: float
    target: float
    kind: str
    subtitle_crossing: bool = False


def seconds_text(t: float, comma: bool = False) -> str:
    ms = max(0, round(t * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{milli:03d}"


def parse_time(s: str) -> float:
    h, m, sec = s.replace(",", ".").split(":")
    if not (0 <= int(m) < 60 and 0 <= float(sec) < 60):
        raise ValueError(s)
    return int(h) * 3600 + int(m) * 60 + float(sec)


TIMING = re.compile(r"^(\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?$")


def read_srt(path: Path, offset: float = 0, encoding: str = "auto") -> tuple[list[Cue], str]:
    data = path.read_bytes()
    encodings = [encoding] if encoding != "auto" else (["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "cp949"])
    decoded = None
    for enc in encodings:
        try:
            decoded = data.decode(enc)
            used = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if decoded is None:
        raise SplitError("자막 인코딩을 읽지 못했습니다. UTF-8 또는 CP949를 선택하세요.")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", decoded.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        match = TIMING.match(lines[0].strip())
        if not match:
            raise SplitError("SRT 시간 형식 오류: " + lines[0][:100])
        try:
            start, end = (parse_time(x) + offset for x in match.groups())
        except ValueError as exc:
            raise SplitError("SRT 시간 범위 오류") from exc
        if end <= start:
            raise SplitError("끝이 시작보다 빠른 SRT 항목이 있습니다.")
        text = "\n".join(lines[1:]).strip()
        if text:
            cues.append(Cue(start, end, text))
    if not cues:
        raise SplitError("자막에 읽을 수 있는 문장이 없습니다.")
    return sorted(cues, key=lambda c: (c.start, c.end)), used


def crossing(cues: list[Cue], t: float) -> bool:
    return any(c.start + 0.001 < t < c.end - 0.001 for c in cues)


def choose_boundary(target: float, scenes: list[float], cues: list[Cue],
                    start: float, duration: float, settings: Settings) -> Boundary:
    # End-exclusive intervals. Never duplicate a tail to provide context.
    lower = max(start + 0.25, target - settings.window)
    upper = min(duration - 0.25, target + settings.window)
    candidates = sorted({round(t, 6) for t in scenes if lower <= t <= upper})
    if candidates:
        safe = [t for t in candidates if not crossing(cues, t)]
        pool = safe if settings.avoid_subtitle and safe else candidates
        chosen = min(pool, key=lambda t: (abs(t - target), t))
        return Boundary(chosen, target, "scene", crossing(cues, chosen))
    if not settings.allow_time_fallback:
        raise SplitError(f"{seconds_text(target)} 앞뒤 {settings.window:g}초에서 전환을 찾지 못했습니다. "
                         "시간 기준 대체를 허용하거나 감지 기준을 낮춰 다시 실행하세요.")
    chosen = target
    if settings.avoid_subtitle and crossing(cues, chosen):
        ends = [c.end + 0.001 for c in cues if lower <= c.end + 0.001 <= upper]
        ends = [t for t in ends if not crossing(cues, t)]
        if ends:
            chosen = min(ends, key=lambda t: abs(t - target))
            return Boundary(chosen, target, "subtitle_end_fallback", False)
    return Boundary(chosen, target, "time_fallback", crossing(cues, chosen))


def write_segment_srt(path: Path, cues: list[Cue], start: float, end: float) -> int:
    selected = []
    for cue in cues:
        a, b = max(cue.start, start), min(cue.end, end)
        if b > a and round((b - start) * 1000) > round((a - start) * 1000):
            selected.append((a - start, b - start, cue.text))
    text = "\n\n".join(f"{i}\n{seconds_text(a, True)} --> {seconds_text(b, True)}\n{s}"
                       for i, (a, b, s) in enumerate(selected, 1))
    path.write_text(text + ("\n" if text else ""), encoding="utf-8-sig")
    return len(selected)


def tools_dir() -> Path:
    return (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent) / "tools"


def find_tool(name: str) -> str:
    bundled = tools_dir() / (name + (".exe" if os.name == "nt" else ""))
    if bundled.is_file():
        return str(bundled)
    installed = shutil.which(name)
    if installed:
        return installed
    raise SplitError(f"{name}가 없습니다. 먼저 SETUP_WINDOWS.cmd를 실행하거나 tools 폴더에 넣어주세요.")


class Runner:
    def __init__(self, cancel: threading.Event | None = None,
                 emit: Callable[[dict], None] | None = None):
        self.cancel = cancel or threading.Event()
        self.emit = emit or (lambda event: None)
        self.log_path: Path | None = None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("사용자가 작업을 중지했습니다.")

    def run(self, args: list[str], label: str = "", total: float = 0,
            line_callback: Callable[[str], None] | None = None) -> str:
        self.check()
        self.emit({"type": "stage", "message": label})
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        events: queue.Queue = queue.Queue()
        def consume(pipe, which):
            try:
                for line in iter(pipe.readline, b""):
                    events.put((which, line.decode("utf-8", errors="replace").rstrip()))
            finally:
                pipe.close()
                events.put((which, None))
        threads = [threading.Thread(target=consume, args=(proc.stdout, "out"), daemon=True),
                   threading.Thread(target=consume, args=(proc.stderr, "err"), daemon=True)]
        for thread in threads:
            thread.start()
        tail = deque(maxlen=35)
        stdout = []
        closed = 0
        log = self.log_path.open("a", encoding="utf-8") if self.log_path else None
        try:
            while closed < 2:
                self.check()
                try:
                    stream, line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    closed += 1
                    continue
                if stream == "err":
                    tail.append(line)
                    if log:
                        log.write(line + "\n")
                else:
                    # Probe JSON is small; progress lines don't need to accumulate.
                    if not total:
                        stdout.append(line)
                    elif line.startswith("out_time_us="):
                        try:
                            fraction = max(0, min(1, int(line.split("=", 1)[1]) / 1e6 / total))
                            self.emit({"type": "progress", "fraction": fraction})
                        except ValueError:
                            pass
                if line_callback:
                    line_callback(line)
            code = proc.wait()
            if code:
                raise SplitError(f"{label} 실패 (종료 코드 {code})\n" + "\n".join(tail))
            return "\n".join(stdout)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for thread in threads:
                thread.join(timeout=2)
            if log:
                log.close()


def probe(path: Path, runner: Runner) -> dict:
    raw = runner.run([find_tool("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                     "영상 정보 확인")
    try:
        data = json.loads(raw)
        videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic", 0)]
        if not videos:
            raise ValueError("no video")
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
        data["video"] = videos[0]
        data["duration_seconds"] = duration
        return data
    except (KeyError, ValueError, TypeError) as exc:
        raise SplitError("영상 길이나 비디오 트랙을 읽지 못했습니다. 손상 여부를 확인하세요.") from exc


def detect_scenes(source: Path, target: float, duration: float, window: float,
                  threshold: float, video_index: int, runner: Runner) -> list[float]:
    # A short preroll establishes the first frame's scene score, not a full movie scan.
    a, b = max(0, target - window - 1), min(duration, target + window + 0.15)
    found = []
    pattern = re.compile(r"\bpts_time:([\d.+eE-]+)")
    def got_line(line):
        if "showinfo" in line:
            match = pattern.search(line)
            if match:
                t = a + float(match.group(1))
                if abs(t - target) <= window + 1e-6:
                    found.append(t)
    runner.run([find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-ss", f"{a:.6f}", "-i", str(source),
                "-t", f"{b-a:.6f}", "-map", f"0:{video_index}", "-an", "-sn", "-dn",
                "-vf", f"scale=320:-2,select='gt(scene,{threshold:.3f})',showinfo",
                "-fps_mode", "vfr", "-f", "null", os.devnull],
               f"장면 전환 찾기 · {seconds_text(target)} ±{window:g}초", line_callback=got_line)
    return found


def safe_stem(name: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:60] or "movie"
    if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        stem = "movie_" + stem
    return stem


def hash_file(path: Path, runner: Runner) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(4 * MB):
            runner.check()
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: dict):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def choose_video_rate(info: dict, source_size: int, audio: dict | None, settings: Settings) -> int:
    raw = info["video"].get("bit_rate")
    try:
        bitrate = int(raw)
    except (ValueError, TypeError):
        audio_total = sum(int(s.get("bit_rate") or 192_000) for s in info["streams"] if s.get("codec_type") == "audio")
        bitrate = int(source_size * 8 / info["duration_seconds"] - audio_total)
    # Analysis copies: no forced 60 fps, no upscaling. A floor avoids unusable rates.
    rate = max(250_000, bitrate)
    if settings.max_height and int(info["video"].get("height", 0)) > settings.max_height:
        rate = max(250_000, int(rate * (settings.max_height / info["video"]["height"]) ** 1.25))
    return rate


def adjusted_rate(current: int, actual_bytes: int, settings: Settings, audio_rate: int = 0) -> int:
    # Leave room for audio and MP4 indexes; never truncate with ffmpeg -fs.
    rate = int((current + audio_rate) * (settings.max_bytes * (1 - settings.margin)) / actual_bytes - audio_rate)
    return max(100_000, min(current - 1000, rate))


def encode_piece(source: Path, start: float, end: float, rate: int, video: dict,
                 audio: dict | None, settings: Settings, work: Path, runner: Runner) -> tuple[Path, dict, int, int]:
    span = end - start
    partial = work / "part.partial.mp4"
    passlog = work / "encode_pass"
    scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if settings.max_height:
        scale = f"scale=-2:'trunc(min(ih,{settings.max_height})/2)*2'"
    ffmpeg = find_tool("ffmpeg")
    for attempt in range(1, 5):
        runner.check()
        common = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                  "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{span:.6f}",
                  "-map", f"0:{video['index']}", "-sn", "-dn", "-vf", scale,
                  "-c:v", "libx264", "-b:v", str(rate), "-preset", settings.preset,
                  "-pix_fmt", "yuv420p", "-fps_mode:v", "vfr",
                  "-passlogfile", str(passlog), "-progress", "pipe:1", "-nostats"]
        runner.run(common + ["-an", "-pass", "1", "-f", "null", os.devnull],
                   f"구간 인코딩 1/2 · 재시도 {attempt-1}", span)
        sound = (["-map", f"0:{audio['index']}", "-c:a", "aac", "-b:a", "192k", "-ac", "2"] if audio else ["-an"])
        runner.run(common + sound + ["-pass", "2", "-map_metadata", "-1", "-map_chapters", "-1",
                                    "-movflags", "+faststart", str(partial)],
                   "구간 인코딩 2/2 · MP4 완성", span)
        size = partial.stat().st_size
        if size > settings.max_bytes:
            if attempt == 4:
                raise SplitError("재시도 후에도 파일 용량이 상한을 넘습니다. 해당 파일을 완료로 처리하지 않았습니다.")
            new_rate = adjusted_rate(rate, size, settings, 192_000 if audio else 0)
            if new_rate >= rate or new_rate < 100_000:
                raise SplitError("용량 상한을 만족시킬 수 없습니다. 더 큰 용량 모드를 선택하세요.")
            runner.emit({"type": "log", "message": f"상한 초과 {size/MB:.2f}MB → 같은 장면 경계를 유지하며 다시 인코딩"})
            rate = new_rate
            continue
        meta = probe(partial, runner)
        # MP4/AAC packet padding and the source frame interval can differ slightly.
        fps_string = video.get("avg_frame_rate", "25/1")
        try:
            n, d = map(float, fps_string.split("/")); frame = d / n
        except (ValueError, ZeroDivisionError):
            frame = 0.04
        tolerance = max(0.15, frame * 3)
        if abs(meta["duration_seconds"] - span) > tolerance:
            raise SplitError(f"구간 길이 검증 실패: 요청 {span:.3f}초 / 출력 {meta['duration_seconds']:.3f}초. 완료 파일로 저장하지 않았습니다.")
        if audio and not any(s.get("codec_type") == "audio" for s in meta["streams"]):
            raise SplitError("출력에서 오디오 트랙이 누락되었습니다.")
        if settings.full_verify:
            runner.run([ffmpeg, "-v", "error", "-xerror", "-nostdin", "-i", str(partial),
                        "-map", "0:v:0", "-map", "0:a?", "-f", "null", os.devnull],
                       "영상·소리 전체 디코딩 검사")
        return partial, meta, rate, attempt - 1
    raise AssertionError("unreachable")


def export_indexes(folder: Path, manifest: dict):
    write_json(folder / "manifest.json", manifest)
    with (folder / "원본_시간표.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["순서", "영상", "원본 시작", "원본 종료(미포함)", "원본 시작 초", "원본 종료 초", "출력 길이 초", "파일 바이트", "MB", "경계 선택", "SHA256"])
        for p in manifest["parts"]:
            writer.writerow([p["number"], p["file"], seconds_text(p["source_start_seconds"]),
                             seconds_text(p["source_end_seconds"]), p["source_start_seconds"], p["source_end_seconds"],
                             p["output_duration_seconds"], p["size_bytes"], round(p["size_bytes"] / MB, 3),
                             p["boundary_kind"], p["sha256"]])
    status = manifest["status"]
    text = ["곰줄 업로드 안내", "", f"상태: {status}", f"원본: {manifest['source']['name']}",
            f"원본 길이: {seconds_text(manifest['source']['duration_seconds'])}",
            f"검증 완료한 분할본: {len(manifest['parts'])}개", "",
            "영상 파일과 원본_시간표.csv 또는 manifest.json을 함께 첨부하세요.",
            "원본 기준 시간 = 분할 영상 안의 시간 + 해당 Part의 원본 시작 시간.",
            "이 시간표는 업로드된 파일 길이를 누적해서 추측하지 않고 원본 분할 구간을 직접 기록합니다.",
            "MP4/오디오 패킷 때문에 출력 길이는 원본 구간 길이와 소폭 다를 수 있습니다.",
            "경계 검증은 구간 연속성·출력 길이·디코딩 검사이며 원본과의 프레임별 동일성 검사는 아닙니다.",
            "업로드 순서는 자유지만 Part 번호를 바꾸지 마세요.", "",
            "모든 Part는 하나의 영화입니다. 결말까지 함께 분석하고 보조 컷도 모든 Part에서 찾으세요.",
            "타임코드는 이 시간표의 source_start_seconds를 더한 원본 기준으로 표시하세요.",
            "아직 최종 편집이나 전체 대본은 작성하지 마세요.", "",
            "파일 크기 검증은 플랫폼의 업로드 성공을 보장하지 않습니다."]
    if status != "completed":
        text += ["", "작업이 완성되지 않았습니다. 현재 파일들을 전체 영화라고 전달하지 마세요."]
    if manifest.get("subtitle"):
        text += ["", "자막: original_subtitles.srt는 수정하지 않은 원본입니다.",
                 "영상과 이름이 같은 SRT는 각 Part에서 0초부터 쓰는 분할 자막입니다.",
                 "자막을 분할했다고 원본 자막의 싱크 오류가 자동으로 해결되지는 않습니다."]
    text += ["", "확인 사항:"] + manifest.get("warnings", [])
    (folder / "업로드_안내.txt").write_text("\n".join(text), encoding="utf-8-sig")


@contextmanager
def job_lock(path: Path):
    """OS releases the lock even after a crash; never delete the lock inode."""
    with path.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SplitError("같은 영화와 설정의 작업이 이미 실행 중입니다.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def split_movie(source: Path, output_parent: Path, settings: Settings,
                subtitle: Path | None = None, runner: Runner | None = None) -> Path:
    settings.validate()
    runner = runner or Runner()
    source = source.expanduser().resolve()
    subtitle = subtitle.expanduser().resolve() if subtitle else None
    if not source.is_file():
        raise SplitError("원본 영화를 선택하세요.")
    if subtitle and not subtitle.is_file():
        raise SplitError("자막 파일을 찾지 못했습니다.")
    runner.emit({"type": "stage", "message": "원본 확인 · 이어갈 작업 검색"})
    source_hash = hash_file(source, runner)
    subtitle_hash = hash_file(subtitle, runner) if subtitle else None
    signature = {"source_sha256": source_hash, "subtitle_sha256": subtitle_hash,
                 "settings": asdict(settings), "engine": "0.2"}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    output_parent = output_parent.expanduser().resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    folder = output_parent / f"{safe_stem(source.stem)}_곰줄_{key}"
    with job_lock(output_parent / f".gomjul_{key}.lock"):
        return _split_movie(source, output_parent, settings, subtitle, runner,
                            folder, signature)


def _split_movie(source: Path, output_parent: Path, settings: Settings,
                 subtitle: Path | None, runner: Runner, folder: Path,
                 signature: dict) -> Path:
    settings.validate()
    runner = runner or Runner()
    source = source.expanduser().resolve()
    output_parent = output_parent.expanduser().resolve()
    if not source.is_file():
        raise SplitError("원본 영화를 선택하세요.")
    if subtitle and not subtitle.is_file():
        raise SplitError("자막 파일을 찾지 못했습니다.")
    info = probe(source, runner)
    video = info["video"]
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        raise SplitError("이 파일은 HDR 영상입니다. 현재 버전은 HDR 색상 변환을 지원하지 않아 중단합니다. SDR 분석본을 사용하세요.")
    tracks = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    audio = next((s for s in tracks if s["index"] == settings.audio_index), None) if settings.audio_index is not None else (tracks[0] if tracks else None)
    if settings.audio_index is not None and audio is None:
        raise SplitError("선택한 오디오 트랙이 원본에 없습니다.")
    cues, encoding = (read_srt(subtitle, settings.subtitle_offset, settings.subtitle_encoding) if subtitle else ([], None))
    duration = info["duration_seconds"]
    rate = choose_video_rate(info, source.stat().st_size, audio, settings)
    total_rate = rate + (192_000 if audio else 0)
    budget = settings.max_bytes * (1 - settings.margin)
    ideal_span = budget * 8 / total_rate
    if ideal_span < 0.5:
        raise SplitError("이 영상의 비트레이트에 비해 용량 상한이 너무 작습니다.")
    output_parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if (folder / "manifest.json").exists():
        previous = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if previous.get("job_signature") != signature:
            raise SplitError("이전 작업의 원본 또는 설정이 다릅니다. 다른 저장 위치를 선택하세요.")
    elif folder.exists() and any(folder.iterdir()):
        raise SplitError("작업 기록 없는 결과 폴더가 있습니다. 다른 저장 위치를 선택하세요.")
    completed_end = previous["parts"][-1]["source_end_seconds"] if previous and previous["parts"] else 0
    estimated_total = max(0, duration - completed_end) * total_rate / 8
    required = int(estimated_total * 1.15 + settings.max_bytes * 2) if estimated_total else 0
    if shutil.disk_usage(output_parent).free < required:
        raise SplitError(f"저장 공간이 부족합니다. 약 {required / 1e9:.2f}GB 이상 확보하세요.")
    folder.mkdir(exist_ok=True)
    runner.log_path = folder / "processing.log"
    warnings = []
    if len(tracks) > 1:
        warnings.append(f"오디오 {len(tracks)}개 중 스트림 {audio['index']} 하나를 AAC 스테레오로 출력했습니다.")
    if cues and max(c.end for c in cues) > duration + 2:
        warnings.append("자막 끝이 영상보다 늦습니다. 판본·싱크 확인이 필요하며 영상 밖 자막은 분할 SRT에 포함되지 않습니다.")
    if any(c.end - c.start > 20 for c in cues):
        warnings.append("20초 이상 표시되는 자막이 있습니다. 자막으로 추정한 대사 구간은 실제 발화와 다를 수 있습니다.")
    if any(c.start < 0 for c in cues):
        warnings.append("보정 후 0초 이전으로 간 자막은 화면에 있는 부분만 포함했습니다.")
    manifest = {"schema_version": 2, "app_version": "0.2.0", "status": "processing",
                "job_signature": signature,
                "source": {"name": source.name, "size_bytes": source.stat().st_size,
                           "sha256": signature["source_sha256"],
                           "duration_seconds": duration, "format_start_time": info["format"].get("start_time"),
                           "video_stream": video["index"], "audio_stream": audio["index"] if audio else None,
                           "frame_rate": video.get("avg_frame_rate"), "width": video.get("width"), "height": video.get("height")},
                "settings": asdict(settings), "parts": [], "warnings": warnings,
                "time_basis": "seconds from beginning of source file, half-open intervals [start,end)",
                "subtitle": {"name": subtitle.name, "encoding": encoding, "offset_seconds": settings.subtitle_offset,
                             "cue_count": len(cues)} if subtitle else None}
    if previous:
        manifest = previous
        warnings = manifest["warnings"]
    runner.emit({"type": "folder", "path": str(folder)})
    try:
        start, number = 0.0, 1
        for part in manifest["parts"]:
            runner.check()
            expected_name = f"Part_{number:03d}.mp4"
            if (part["file"] != expected_name or part["number"] != number
                    or abs(part["source_start_seconds"] - start) > 2e-6
                    or not start < part["source_end_seconds"] <= duration + 2e-6):
                raise SplitError("이전 작업 시간표가 올바르지 않습니다. 다른 저장 위치를 선택하세요.")
            existing = folder / expected_name
            runner.emit({"type": "stage", "message": f"완료 파일 확인 · {expected_name}"})
            if (not existing.is_file() or existing.stat().st_size != part["size_bytes"]
                    or existing.stat().st_size > settings.max_bytes
                    or hash_file(existing, runner) != part["sha256"]):
                raise SplitError(f"{expected_name}이 없거나 변경되었습니다. 보존을 위해 중단합니다. 새 저장 위치에서 다시 시작하세요.")
            if subtitle:
                # Regenerate optional sidecars from the explicitly selected SRT.
                write_segment_srt(existing.with_suffix(".srt"), cues, start, part["source_end_seconds"])
            runner.emit({"type": "part_done", "part": part})
            start, number = part["source_end_seconds"], number + 1
        if number > 1:
            runner.emit({"type": "log", "message": f"검증된 {number-1}개 파일을 재사용합니다. {seconds_text(start)}부터 이어갑니다."})
        # A crash may leave a promoted but not yet committed part. Preserve it,
        # then regenerate that part; never reuse unverified output.
        committed = {p["file"] for p in manifest["parts"]}
        orphans = [p for p in folder.glob("Part_*.mp4") if p.name not in committed]
        if orphans:
            recovery = Path(tempfile.mkdtemp(prefix="미완료_보관_", dir=folder))
            for item in orphans:
                item.replace(recovery / item.name)
                sidecar = item.with_suffix(".srt")
                if sidecar.exists():
                    sidecar.replace(recovery / sidecar.name)
        manifest["status"] = "processing"
        manifest.pop("error", None)
        export_indexes(folder, manifest)
        if subtitle:
            shutil.copyfile(subtitle, folder / "original_subtitles.srt")
        while start < duration - 1e-6:
            runner.check()
            remaining = duration - start
            target = min(duration, start + ideal_span)
            if remaining <= ideal_span * 1.02:
                boundary = Boundary(duration, duration, "end_of_file")
            else:
                scenes = detect_scenes(source, target, duration, settings.window,
                                       settings.scene_threshold, video["index"], runner)
                boundary = choose_boundary(target, scenes, cues, start, duration, settings)
            end = boundary.time
            if not start < end <= duration:
                raise SplitError("분할 구간이 순서대로 이어지지 않습니다.")
            runner.emit({"type": "part_start", "number": number, "start": start, "end": end,
                         "message": f"Part {number:03d} · {seconds_text(start)} → {seconds_text(end)}"})
            if "fallback" in boundary.kind:
                note = f"Part {number:03d} 끝: 장면 전환 없음 → {boundary.kind} ({seconds_text(end)})"
                warnings.append(note)
                runner.emit({"type": "log", "message": note})
            if boundary.subtitle_crossing:
                warnings.append(f"Part {number:03d} 경계에 자막이 걸쳐 있습니다. 해당 문장을 양쪽 SRT에 구간별로 나눴습니다.")
            with tempfile.TemporaryDirectory(prefix="_processing_", dir=folder) as td:
                partial, meta, final_rate, retries = encode_piece(source, start, end, rate, video, audio,
                                                                 settings, Path(td), runner)
                filename = f"Part_{number:03d}.mp4"
                destination = folder / filename
                if destination.exists():
                    raise SplitError("기존 결과 파일을 덮어쓰지 않았습니다.")
                # Only a finalized, size-checked, duration-checked MP4 is promoted.
                partial.replace(destination)
            part = {"number": number, "file": filename,
                    "source_start_seconds": round(start, 6), "source_end_seconds": round(end, 6),
                    "source_duration_seconds": round(end-start, 6),
                    "output_duration_seconds": meta["duration_seconds"],
                    "output_start_time": meta["format"].get("start_time"),
                    "size_bytes": destination.stat().st_size,
                    "boundary_kind": boundary.kind, "boundary_target_seconds": round(boundary.target, 6),
                    "boundary_shift_seconds": round(end - boundary.target, 6),
                    "subtitle_crossing": boundary.subtitle_crossing,
                    "video_bitrate": final_rate, "size_retries": retries,
                    "full_decode_verified": settings.full_verify,
                    "sha256": hash_file(destination, runner)}
            if subtitle:
                srt_path = destination.with_suffix(".srt")
                part["subtitle_file"] = srt_path.name
                part["subtitle_cues"] = write_segment_srt(srt_path, cues, start, end)
            manifest["parts"].append(part)
            export_indexes(folder, manifest)
            runner.emit({"type": "part_done", "part": part})
            start, number = end, number + 1
        parts = manifest["parts"]
        if not parts or abs(parts[0]["source_start_seconds"]) > 1e-6 or abs(parts[-1]["source_end_seconds"]-duration) > 2e-6:
            raise SplitError("원본 전체 범위 검증에 실패했습니다.")
        if any(abs(a["source_end_seconds"] - b["source_start_seconds"]) > 2e-6 for a, b in zip(parts, parts[1:])):
            raise SplitError("원본 구간 사이에 누락 또는 중복이 있습니다.")
        if any(p["size_bytes"] > settings.max_bytes for p in parts):
            raise SplitError("용량 상한 검증 실패")
        manifest["status"] = "completed"
        manifest["planned_source_coverage_verified"] = True
        manifest["all_parts_under_limit"] = True
        manifest["output_duration_sum_seconds"] = sum(p["output_duration_seconds"] for p in parts)
        export_indexes(folder, manifest)
        runner.emit({"type": "done", "path": str(folder), "count": len(parts), "has_subtitle": bool(subtitle)})
        return folder
    except BaseException as exc:
        manifest["status"] = "cancelled" if isinstance(exc, Cancelled) else "failed"
        manifest["error"] = str(exc)
        export_indexes(folder, manifest)
        raise


def main():
    parser = argparse.ArgumentParser(description="곰줄 영화 분할기 — 장면 탐색, MP4 용량 검사, 원본 시간표")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--max-mb", type=float, default=250)
    parser.add_argument("--window", type=float, default=10)
    parser.add_argument("--threshold", type=float, default=.30)
    parser.add_argument("--subtitle-offset", type=float, default=0)
    parser.add_argument("--strict-scenes", action="store_true")
    parser.add_argument("--height", type=int, choices=[0, 720, 1080], default=0)
    parser.add_argument("--audio-index", type=int)
    args = parser.parse_args()
    settings = Settings(max_bytes=round(args.max_mb * MB), window=args.window,
                        scene_threshold=args.threshold, subtitle_offset=args.subtitle_offset,
                        allow_time_fallback=not args.strict_scenes, max_height=args.height,
                        audio_index=args.audio_index)
    def emit(event):
        if event["type"] != "progress":
            print(json.dumps(event, ensure_ascii=False), flush=True)
    try:
        split_movie(args.source, args.output, settings, args.srt, Runner(emit=emit))
    except (SplitError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
