"""Optional real GLSL rendering; default tests do not require EGL or a GPU.

Run all real-render tests with:
    AMV_TEST_EGL=1 .venv/bin/python -m pytest -q tests/test_cloud_shaders.py
An explicit opt-in must fail, rather than skip, if EGL cannot initialize.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import struct
import zlib

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("amv_cloud_shader_tool", ROOT / "tools/render_shaders.py")
shader_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shader_tool)


def test_scene_and_mode_enums_match_schema_and_builder():
    schema = json.loads((ROOT / "director_schema.json").read_text())["properties"]
    assert tuple(schema["scene"]["enum"]) == shader_tool.SCENES
    assert tuple(schema["palette"]["enum"]) == shader_tool.PALETTES
    assert tuple(schema["particle_mode"]["enum"]) == shader_tool.PARTICLE_MODES
    builder_spec = importlib.util.spec_from_file_location("amv_cloud_td_builder", ROOT / "td/build_network.py")
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)
    assert builder.IMPLEMENTED_SCENES == shader_tool.SCENES


def test_png_writer_preserves_dimensions_and_rgb_bytes():
    frame = np.array([[[0, 0.5, 1, 1], [1, 0, 0.25, 1]]], dtype=np.float32)
    data = shader_tool.png_bytes(frame)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (2, 1)
    offset, decoded = 8, b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        checksum = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        assert checksum == zlib.crc32(kind + payload) & 0xFFFFFFFF
        if kind == b"IDAT":
            decoded += payload
        offset += length + 12
    assert zlib.decompress(decoded) == bytes([0, 0, 128, 255, 255, 0, 64])


@pytest.mark.parametrize("bad_value", [np.nan, np.inf, -1.0, 2.0])
def test_float_validation_rejects_invalid_output(bad_value):
    frame = np.ones((2, 2, 4), dtype=np.float32)
    frame[0, 0, 0] = bad_value
    with pytest.raises(AssertionError):
        shader_tool.assert_valid_frame(frame, patterned=False)


def test_float_validation_rejects_unrendered_alpha():
    frame = np.ones((2, 2, 4), dtype=np.float32)
    frame[0, 0, 3] = -1
    with pytest.raises(AssertionError, match="Alpha"):
        shader_tool.assert_valid_frame(frame, patterned=False)


def test_pattern_check_is_explicitly_optional_for_particle_none():
    frame = np.full((2, 2, 4), 0.03, dtype=np.float32)
    frame[..., 3] = 1
    with pytest.raises(AssertionError, match="Spatially constant"):
        shader_tool.assert_valid_frame(frame)
    shader_tool.assert_valid_frame(frame, patterned=False)


requires_egl = pytest.mark.skipif(
    os.environ.get("AMV_TEST_EGL") != "1",
    reason="Opt in with AMV_TEST_EGL=1 on a host with surfaceless EGL/OpenGL",
)


@requires_egl
def test_actual_shader_full_render_matrix():
    with shader_tool.ShaderRenderer(160, 90) as renderer:
        rows, previews = shader_tool.verify_matrix(renderer)
        assert len(rows) == len(previews) == 5
        assert sum(len(row["cases"]) for row in rows) == 85
        assert renderer.info["gl_renderer"]


@requires_egl
@pytest.mark.parametrize("dimensions", [(161, 91), (128, 128), (1280, 720)])
def test_all_scenes_at_odd_square_and_td_output_dimensions(dimensions):
    with shader_tool.ShaderRenderer(*dimensions) as renderer:
        for scene in range(5):
            frame = renderer.render(scene)
            assert frame.shape == (dimensions[1], dimensions[0], 4)
            shader_tool.assert_valid_frame(frame)


@requires_egl
def test_compile_failure_is_not_reported_as_success(tmp_path):
    shader = tmp_path / "invalid.frag"
    shader.write_text("THIS IS NOT VALID GLSL\n")
    with pytest.raises(RuntimeError, match="Shader compilation failed"):
        with shader_tool.ShaderRenderer(8, 8, shader):
            pass
    # The failed attempt must release its context so the next run still works.
    with shader_tool.ShaderRenderer(32, 18) as renderer:
        shader_tool.assert_valid_frame(renderer.render())


@requires_egl
def test_artifact_report_preserves_source_and_scope(tmp_path):
    original = shader_tool.SHADER_PATH.read_bytes()
    with shader_tool.ShaderRenderer(80, 45) as renderer:
        rows, previews = shader_tool.verify_matrix(renderer)
        report = shader_tool.write_artifacts(tmp_path, renderer, rows, previews)
    assert shader_tool.SHADER_PATH.read_bytes() == original
    assert report["shader_source_modified"] is False
    assert report["rendered_frame_count"] == 120
    assert report["passed"] is True
    assert (tmp_path / "contact-sheet.png").is_file()
    assert (tmp_path / "contact-sheet.svg").is_file()
    assert json.loads((tmp_path / "report.json").read_text())["passed"] is True
