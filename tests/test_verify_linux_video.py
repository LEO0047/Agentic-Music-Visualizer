"""Pure helper tests and a short, real encode/decode test for video auditing."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("amv_verify_video", ROOT / "tools/verify_linux_video.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def test_exact_read_accepts_short_pipe_reads():
    class ShortRead(io.BytesIO):
        def read(self, count=-1):
            return super().read(min(count, 2))

    stream = ShortRead(b"abcdefgh")
    assert audit.read_exact(stream, 6) == b"abcdef"
    assert audit.read_exact(stream, 2) == b"gh"
    assert audit.read_exact(stream, 2) == b""


def test_exact_read_rejects_truncated_frame():
    with pytest.raises(ValueError, match="Truncated"):
        audit.read_exact(io.BytesIO(b"12345"), 6)


def test_pixel_delta_avoids_unsigned_wrap():
    black = np.zeros((2, 3, 3), dtype=np.uint8)
    white = np.full_like(black, 255)
    assert audit.pixel_metrics(black, white)["mean_abs_rgb_delta"] == 1
    assert audit.pixel_metrics(white, black)["mean_abs_rgb_delta"] == 1
    assert audit.pixel_metrics(black, black)["exact_repeat"]
    assert audit.pixel_metrics(black)["near_black_fraction"] == 1


def test_timestamp_gaps_and_repeats_are_detected():
    good = [{"best_effort_timestamp_time": f"{i / 30:.6f}"} for i in range(4)]
    assert audit.inspect_timestamps(good, 30)["constant_frame_spacing"]
    bad = good[:2] + good[3:]
    assert not audit.inspect_timestamps(bad, 30)["constant_frame_spacing"]
    assert not audit.inspect_timestamps(good + good[-1:], 30)["strictly_increasing"]
    assert not audit.inspect_timestamps(good + [{}], 30)["all_frames_have_timestamp"]


def test_repeat_duration_includes_original_and_repeated_frames():
    frame = np.zeros((2, 3, 3), dtype=np.uint8)
    rows = [{"frame": i, "time_seconds": i / 10,
             **audit.pixel_metrics(frame, None if i == 0 else frame)} for i in range(12)]
    result = audit.summarize_frames(rows, 10)
    assert result["decoded_frame_count"] == 12
    assert result["longest_exact_repeat_seconds"] == 1.2
    assert result["zero_rgb_frames"] == list(range(12))


def test_audio_decode_handles_chunk_splits_and_final_partial_window(monkeypatch):
    samples = np.array([[0.1, -0.1], [0.2, -0.2], [0.3, -0.3],
                        [0.4, -0.4], [0.5, -0.5]], dtype="<f4")

    class ShortRead(io.BytesIO):
        def read(self, count=-1):
            # Deliberately split float and channel boundaries.
            return super().read(min(count, 3))

    class Process:
        stdout = ShortRead(samples.tobytes())

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(audit.subprocess, "Popen", lambda *args, **kwargs: Process())
    result = audit.decode_audio("unused.mp4", {"sample_rate": "20", "channels": 2})
    assert result["decoded_sample_frames"] == 5
    assert result["decoded_duration_seconds"] == 0.25
    assert result["peak_absolute"] == pytest.approx(0.5)
    assert result["rms"] == pytest.approx(float(np.sqrt(np.mean(samples ** 2))))
    assert len(result["rms_100ms_windows"]) == 3
    assert result["rms_100ms_windows"][-1]["time_seconds"] == 0.2
    assert result["rms_100ms_windows"][-1]["rms"] == pytest.approx(0.5)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg tools required")
def test_real_video_audit_decodes_every_frame_and_audio(tmp_path):
    video = tmp_path / "fixture.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=10:duration=1.2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.2",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video),
    ], check=True)
    report = audit.verify_video(video, tmp_path / "good", expected_fps=10,
                                expected_duration=1.2, expected_frames=12, require_audio=True,
                                sample_times=[0.3, 0.7])
    assert report["passed_media_integrity"]
    assert report["frame_summary"]["decoded_frame_count"] == 12
    assert report["audio"]["decoded_sample_frames"] >= 48000
    assert report["audio"]["rms"] > 0
    assert (tmp_path / "good/decoded-contact-sheet.png").is_file()
    wrong = audit.verify_video(video, tmp_path / "bad", expected_frames=13)
    assert not wrong["passed_media_integrity"]
    assert not wrong["checks"]["expected_frame_count"]
