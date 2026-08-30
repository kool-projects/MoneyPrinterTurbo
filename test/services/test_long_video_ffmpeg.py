"""Offline integration checks: actual FFmpeg and MoviePy, no paid providers."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from app.config import config
from app.models.schema import MaterialInfo, VideoParams
from app.services import long_video as lv, task as tm, video
from app.services.state import MemoryState

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE, reason="requires ffmpeg and ffprobe"
)


def run(*args):
    return subprocess.run(args, capture_output=True, check=True).stdout


def probe(filename):
    return json.loads(
        run(
            FFPROBE,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(filename),
        )
    )


def make_chapter(filename, color, frequency, duration, fps=30):
    run(
        FFMPEG,
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=160x90:r={fps}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:sample_rate=44100",
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(filename),
    )


def test_concat_keeps_audio_video_order_and_handles_quoted_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(video.utils, "get_ffmpeg_binary", lambda: FFMPEG)
    first = tmp_path / "first chapter's video.mp4"
    second = tmp_path / "segundo capítulo.mp4"
    output = tmp_path / "final.mp4"
    make_chapter(first, "red", 440, 1.3)
    make_chapter(second, "blue", 880, 1.7)
    video.concat_chapters_with_ffmpeg([str(first), str(second)], str(output))
    info = probe(output)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - 3) < 0.2
    for when, expected_color, expected_frequency in [(0.5, 0, 440), (2.0, 2, 880)]:
        frame = run(
            FFMPEG,
            "-v",
            "error",
            "-ss",
            str(when),
            "-i",
            str(output),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        )
        mean = np.frombuffer(frame, dtype=np.uint8).reshape(-1, 3).mean(axis=0)
        assert mean.argmax() == expected_color
        audio = run(
            FFMPEG,
            "-v",
            "error",
            "-ss",
            str(when),
            "-i",
            str(output),
            "-t",
            "0.2",
            "-vn",
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-",
        )
        samples = np.frombuffer(audio, dtype=np.float32)
        frequency = np.fft.rfftfreq(len(samples), 1 / 44100)[
            np.abs(np.fft.rfft(samples)).argmax()
        ]
        assert abs(frequency - expected_frequency) < 10
    run(FFMPEG, "-v", "error", "-i", str(output), "-f", "null", "-")
    assert not list(tmp_path.glob(".chapter-concat-*"))


def test_concat_failure_preserves_existing_final(tmp_path, monkeypatch):
    monkeypatch.setattr(video.utils, "get_ffmpeg_binary", lambda: FFMPEG)
    bad = tmp_path / "bad.mp4"
    bad.write_text("not a video")
    output = tmp_path / "final.mp4"
    output.write_bytes(b"previous output")
    with pytest.raises(RuntimeError, match="concat failed"):
        video.concat_chapters_with_ffmpeg([str(bad)], str(output))
    assert output.read_bytes() == b"previous output"
    assert not list(tmp_path.glob(".chapter-concat-*"))


@pytest.mark.parametrize("total", [600, 1800])
@pytest.mark.skipif(
    os.environ.get("MPT_LONG_STRESS_TESTS") != "1",
    reason="opt-in 10/30-minute container test",
)
def test_long_duration_concat(tmp_path, monkeypatch, total):
    monkeypatch.setattr(video.utils, "get_ffmpeg_binary", lambda: FFMPEG)
    chapters = []
    for index, color in enumerate(["red", "green", "blue"]):
        chapter = tmp_path / f"chapter-{index}.mp4"
        make_chapter(chapter, color, 440 * (index + 1), total / 3, fps=2)
        chapters.append(str(chapter))
    output = tmp_path / "final.mp4"
    video.concat_chapters_with_ffmpeg(chapters, str(output))
    info = probe(output)
    assert abs(float(info["format"]["duration"]) - total) < 0.3
    durations = {s["codec_type"]: float(s["duration"]) for s in info["streams"]}
    assert abs(durations["audio"] - durations["video"]) < 0.3
    run(FFMPEG, "-v", "error", "-i", str(output), "-f", "null", "-")


def test_actual_chapter_pipeline_renders_1080p(tmp_path, monkeypatch):
    def task_dir(task_id=""):
        directory = tmp_path / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    monkeypatch.setattr(tm.utils, "task_dir", task_dir)
    monkeypatch.setattr(video.utils, "get_ffmpeg_binary", lambda: FFMPEG)
    monkeypatch.setattr(tm.sm, "state", MemoryState())
    monkeypatch.setattr(lv, "MAX_BLOCK_WORDS", 4)
    monkeypatch.setitem(config.app, "video_codec", "libx264")

    def storage_dir(sub_dir="", create=False):
        directory = tmp_path / sub_dir
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    monkeypatch.setattr(tm.utils, "storage_dir", storage_dir)
    stock = Path(storage_dir("local_videos", create=True)) / "stock.mp4"
    run(
        FFMPEG,
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=green:s=864x480:r=30",
        "-t",
        "2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(stock),
    )

    def synthetic_tts(text, voice_name, voice_rate, voice_file):
        run(
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1.2",
            str(voice_file),
        )
        return object()

    monkeypatch.setattr(tm.voice, "tts", synthetic_tts)
    params = VideoParams(
        video_subject="Offline smoke test",
        target_duration_minutes=10,
        video_script="First chapter has words. Second chapter has words.",
        video_source="local",
        video_materials=[MaterialInfo(provider="local", url=str(stock))],
        bgm_type="",
        subtitle_enabled=False,
        n_threads=2,
    )
    result = tm.start("smoke", params)
    assert "videos" in result, result
    assert len(result["chapters"]) == 2
    info = probe(result["videos"][0])
    stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (stream["width"], stream["height"]) == (1920, 1080)
    assert 2.4 <= float(info["format"]["duration"]) < 3.5
    assert result["warnings"][0]["code"] == "target_duration_mismatch"
    for ch in result["chapters"]:
        assert Path(ch["audio_file"]).is_file()
        assert Path(ch["videos"][0]).is_file()
