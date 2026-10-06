#!/usr/bin/env python3
"""Render the unmodified TD fragment shader with surfaceless EGL/OpenGL.

Uses Python's ctypes, installed EGL/GL system libraries, and the project's numpy
only. This is shader portability/finite-output evidence, NOT TouchDesigner,
Metal, audio-device, post-processing, or real-time GPU performance validation.

    .venv/bin/python tools/render_shaders.py
    AMV_TEST_EGL=1 .venv/bin/python -m pytest -q tests/test_cloud_shaders.py

The TD host provides vUV and TDOutputSwizzle; this harness supplies normalized
UVs and an identity RGBA swizzle. Actual shader bytes are otherwise unchanged.
"""
from __future__ import annotations

import argparse
import base64
import ctypes as ct
import ctypes.util
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import sys
import zlib

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SHADER_PATH = ROOT / "td/shaders/visualizer.frag"
SCENES = ("fractal_temple", "tunnel", "particle_field", "kaleido_mesh", "projectm_blend")
PALETTES = ("violet_cyan", "acid_lime", "amber_dusk", "mono_white", "infrared")
PARTICLE_MODES = ("spiral", "burst", "rain", "orbit", "none")
VERTEX_SOURCE = """#version 330 core
out vec3 vUV;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    vUV = vec3(p, 0.0);
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
TD_PREFIX = """#version 330 core
in vec3 vUV;
vec4 TDOutputSwizzle(vec4 value) { return value; }
#line 1
"""


class EGLUnavailable(RuntimeError):
    """System EGL/GL cannot create the requested offscreen core context."""


def _bind(lib, name, result, *args):
    function = getattr(lib, name)
    function.restype = result
    function.argtypes = list(args)
    return function


class ShaderRenderer:
    """One offscreen GL context, one program and an RGBA32F framebuffer.

    Use on one thread, inside a with statement; no GL resources outlive it.
    """

    def __init__(self, width=320, height=180, shader_path=SHADER_PATH):
        if width <= 0 or height <= 0:
            raise ValueError("Render dimensions must be positive")
        self.width, self.height = width, height
        self.display = self.context = self.surface = None
        self.program = self.texture = self.framebuffer = self.vao = 0
        self.shader_path = Path(shader_path)
        self.shader_bytes = self.shader_path.read_bytes()
        # Avoid writing a default Mesa cache outside the caller's workspace.
        os.environ.setdefault("MESA_SHADER_CACHE_DISABLE", "true")
        egl_name = ctypes.util.find_library("EGL")
        gl_name = ctypes.util.find_library("GL")
        if not egl_name or not gl_name:
            raise EGLUnavailable("Installed libEGL and libGL are required")
        try:
            self.egl, self.gl = ct.CDLL(egl_name), ct.CDLL(gl_name)
            self._bind_egl()
            self._create_context()
            self._bind_gl()
            self.info = {
                "egl_version": self.eglQueryString(self.display, 0x3054).decode(),
                "egl_vendor": self.eglQueryString(self.display, 0x3053).decode(),
                "gl_vendor": self.glGetString(0x1F00).decode(),
                "gl_renderer": self.glGetString(0x1F01).decode(),
                "gl_version": self.glGetString(0x1F02).decode(),
                "glsl_version": self.glGetString(0x8B8C).decode(),
            }
            self.program = self._program(TD_PREFIX + self.shader_bytes.decode())
            self._create_target()
        except Exception:
            self.close()
            raise

    def _bind_egl(self):
        p, i, u = ct.c_void_p, ct.c_int, ct.c_uint
        for name, result, args in [
            ("eglGetPlatformDisplay", p, (u, p, ct.POINTER(i))),
            ("eglInitialize", u, (p, ct.POINTER(i), ct.POINTER(i))),
            ("eglBindAPI", u, (u,)),
            ("eglChooseConfig", u, (p, ct.POINTER(i), ct.POINTER(p), i, ct.POINTER(i))),
            ("eglCreateContext", p, (p, p, p, ct.POINTER(i))),
            ("eglCreatePbufferSurface", p, (p, p, ct.POINTER(i))),
            ("eglMakeCurrent", u, (p, p, p, p)),
            ("eglDestroyContext", u, (p, p)),
            ("eglDestroySurface", u, (p, p)),
            ("eglTerminate", u, (p,)),
            ("eglQueryString", ct.c_char_p, (p, i)),
            ("eglGetError", u, ()),
        ]:
            try:
                setattr(self, name, _bind(self.egl, name, result, *args))
            except AttributeError as exc:
                raise EGLUnavailable(f"System EGL lacks {name}") from exc

    def _egl_check(self, value, operation):
        if not value:
            raise EGLUnavailable(f"{operation} failed: EGL 0x{self.eglGetError():04x}")
        return value

    def _create_context(self):
        i, p = ct.c_int, ct.c_void_p
        # EGL_PLATFORM_SURFACELESS_MESA, independent of DISPLAY or desktop.
        self.display = self._egl_check(
            self.eglGetPlatformDisplay(0x31DD, None, None), "surfaceless display"
        )
        major, minor = i(), i()
        self._egl_check(self.eglInitialize(self.display, ct.byref(major), ct.byref(minor)), "initialize")
        self._egl_check(self.eglBindAPI(0x30A2), "OpenGL API")
        attributes = (i * 13)(0x3033, 1, 0x3040, 8, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3038)
        config, count = p(), i()
        self._egl_check(self.eglChooseConfig(self.display, attributes, ct.byref(config), 1, ct.byref(count)), "choose config")
        self._egl_check(count.value, "OpenGL pbuffer configuration")
        context_attributes = (i * 7)(0x3098, 3, 0x30FB, 3, 0x30FD, 1, 0x3038)
        self.context = self._egl_check(self.eglCreateContext(self.display, config, None, context_attributes), "OpenGL 3.3 core context")
        surface_attributes = (i * 5)(0x3057, 1, 0x3056, 1, 0x3038)
        self.surface = self._egl_check(self.eglCreatePbufferSurface(self.display, config, surface_attributes), "pbuffer")
        self._egl_check(self.eglMakeCurrent(self.display, self.surface, self.surface, self.context), "make current")

    def _bind_gl(self):
        u, i, f, p = ct.c_uint, ct.c_int, ct.c_float, ct.c_void_p
        pi, pu = ct.POINTER(i), ct.POINTER(u)
        specifications = [
            ("glGetString", ct.c_char_p, (u,)),
            ("glGetError", u, ()),
            ("glCreateShader", u, (u,)),
            ("glShaderSource", None, (u, i, ct.POINTER(ct.c_char_p), pi)),
            ("glCompileShader", None, (u,)),
            ("glGetShaderiv", None, (u, u, pi)),
            ("glGetShaderInfoLog", None, (u, i, pi, p)),
            ("glDeleteShader", None, (u,)),
            ("glCreateProgram", u, ()),
            ("glAttachShader", None, (u, u)),
            ("glLinkProgram", None, (u,)),
            ("glGetProgramiv", None, (u, u, pi)),
            ("glGetProgramInfoLog", None, (u, i, pi, p)),
            ("glDeleteProgram", None, (u,)),
            ("glUseProgram", None, (u,)),
            ("glGetUniformLocation", i, (u, ct.c_char_p)),
            ("glUniform4f", None, (i, f, f, f, f)),
            ("glGenVertexArrays", None, (i, pu)),
            ("glBindVertexArray", None, (u,)),
            ("glDeleteVertexArrays", None, (i, pu)),
            ("glGenTextures", None, (i, pu)),
            ("glBindTexture", None, (u, u)),
            ("glTexImage2D", None, (u, i, i, i, i, i, u, u, p)),
            ("glTexParameteri", None, (u, u, i)),
            ("glDeleteTextures", None, (i, pu)),
            ("glGenFramebuffers", None, (i, pu)),
            ("glBindFramebuffer", None, (u, u)),
            ("glFramebufferTexture2D", None, (u, u, u, u, i)),
            ("glCheckFramebufferStatus", u, (u,)),
            ("glDeleteFramebuffers", None, (i, pu)),
            ("glViewport", None, (i, i, i, i)),
            ("glClearColor", None, (f, f, f, f)),
            ("glClear", None, (u,)),
            ("glDrawArrays", None, (u, i, i)),
            ("glReadPixels", None, (i, i, i, i, u, u, p)),
        ]
        for name, result, args in specifications:
            setattr(self, name, _bind(self.gl, name, result, *args))

    def _compile(self, kind, source):
        shader = self.glCreateShader(kind)
        encoded = ct.c_char_p(source.encode())
        self.glShaderSource(shader, 1, ct.byref(encoded), None)
        self.glCompileShader(shader)
        status = ct.c_int()
        self.glGetShaderiv(shader, 0x8B81, ct.byref(status))
        if not status.value:
            log = ct.create_string_buffer(65536)
            self.glGetShaderInfoLog(shader, len(log), None, log)
            self.glDeleteShader(shader)
            raise RuntimeError(f"Shader compilation failed: {log.value.decode()}")
        return shader

    def _program(self, fragment_source):
        vertex = self._compile(0x8B31, VERTEX_SOURCE)
        fragment = program = 0
        try:
            fragment = self._compile(0x8B30, fragment_source)
            program = self.glCreateProgram()
            self.glAttachShader(program, vertex)
            self.glAttachShader(program, fragment)
            self.glLinkProgram(program)
            status = ct.c_int()
            self.glGetProgramiv(program, 0x8B82, ct.byref(status))
            if not status.value:
                log = ct.create_string_buffer(65536)
                self.glGetProgramInfoLog(program, len(log), None, log)
                raise RuntimeError(f"Shader link failed: {log.value.decode()}")
            return program
        except Exception:
            if program:
                self.glDeleteProgram(program)
            raise
        finally:
            self.glDeleteShader(vertex)
            if fragment:
                self.glDeleteShader(fragment)

    def _create_target(self):
        for attribute, generate in (("vao", self.glGenVertexArrays), ("texture", self.glGenTextures), ("framebuffer", self.glGenFramebuffers)):
            handle = ct.c_uint()
            generate(1, ct.byref(handle))
            setattr(self, attribute, handle.value)
        self.glBindVertexArray(self.vao)
        self.glBindTexture(0x0DE1, self.texture)
        self.glTexImage2D(0x0DE1, 0, 0x8814, self.width, self.height, 0, 0x1908, 0x1406, None)
        self.glTexParameteri(0x0DE1, 0x2801, 0x2600)
        self.glTexParameteri(0x0DE1, 0x2800, 0x2600)
        self.glBindFramebuffer(0x8D40, self.framebuffer)
        self.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, self.texture, 0)
        status = self.glCheckFramebufferStatus(0x8D40)
        if status != 0x8CD5:
            raise RuntimeError(f"RGBA32F framebuffer incomplete: 0x{status:x}")
        self.glUseProgram(self.program)
        self.locations = {name: self.glGetUniformLocation(self.program, name.encode()) for name in ("uAudio", "uClock", "uModes")}
        if any(value < 0 for value in self.locations.values()):
            raise RuntimeError(f"Required uniforms missing: {self.locations}")
        self._gl_check("initialization")

    def _gl_check(self, operation):
        error = self.glGetError()
        if error:
            raise RuntimeError(f"{operation}: OpenGL error 0x{error:04x}")

    def render(self, scene=0, *, time=12.0, audio=(0.35, 0.45, 0.25, 0.4), speed=0.4, symmetry=8, particle_mode=0, palette=0, kick=0.0):
        """Return top-to-bottom float32 RGBA pixels (before PNG quantization)."""
        self.glUniform4f(self.locations["uAudio"], *audio)
        self.glUniform4f(self.locations["uClock"], time, speed, symmetry, scene)
        self.glUniform4f(self.locations["uModes"], particle_mode, palette, self.width / self.height, kick)
        self.glViewport(0, 0, self.width, self.height)
        # Negative sentinel catches incomplete full-screen coverage.
        self.glClearColor(-1.0, -1.0, -1.0, -1.0)
        self.glClear(0x4000)
        self.glDrawArrays(0x0004, 0, 3)
        frame = np.empty((self.height, self.width, 4), dtype=np.float32)
        self.glReadPixels(0, 0, self.width, self.height, 0x1908, 0x1406, frame.ctypes.data_as(ct.c_void_p))
        self._gl_check("draw/read RGBA32F")
        return frame[::-1].copy()

    def close(self):
        if self.context and hasattr(self, "glDeleteProgram"):
            for attribute, delete in (("vao", self.glDeleteVertexArrays), ("texture", self.glDeleteTextures), ("framebuffer", self.glDeleteFramebuffers)):
                value = ct.c_uint(getattr(self, attribute, 0))
                if value.value:
                    delete(1, ct.byref(value))
                    setattr(self, attribute, 0)
            if self.program:
                self.glDeleteProgram(self.program)
                self.program = 0
        if self.display and hasattr(self, "eglMakeCurrent"):
            self.eglMakeCurrent(self.display, None, None, None)
            if self.surface:
                self.eglDestroySurface(self.display, self.surface)
                self.surface = None
            if self.context:
                self.eglDestroyContext(self.display, self.context)
                self.context = None
            self.eglTerminate(self.display)
            self.display = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def frame_stats(frame):
    rgb = frame[..., :3]
    return {
        "all_finite": bool(np.isfinite(frame).all()),
        "min_rgb": float(rgb.min()),
        "max_rgb": float(rgb.max()),
        "mean_rgb": float(rgb.mean()),
        "spatial_std": float(rgb.std(axis=(0, 1)).mean()),
        "alpha_is_one": bool(np.all(frame[..., 3] == 1.0)),
    }


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def assert_valid_frame(frame, *, patterned=True):
    _require(bool(np.isfinite(frame).all()), "Non-finite float framebuffer output")
    stats = frame_stats(frame)
    _require(0 <= stats["min_rgb"] <= stats["max_rgb"] <= 1.00001, stats)
    _require(stats["alpha_is_one"], "Alpha or full-screen coverage differs")
    _require(stats["max_rgb"] > 0.02, "Blank output")
    if patterned:
        _require(stats["spatial_std"] > 0.001, "Spatially constant output")
    return stats


def frame_delta(first, second):
    return float(np.abs(first[..., :3] - second[..., :3]).mean())


def png_bytes(frame):
    rgb = np.rint(np.clip(frame[..., :3], 0, 1) * 255).astype(np.uint8)
    height, width, _ = rgb.shape

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    pixels = b"".join(b"\x00" + row.tobytes() for row in rgb)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


def verify_matrix(renderer):
    """Exercise every enum, animation/audio, endpoints, and long timestamps."""
    rows, previews = [], []
    for index, name in enumerate(SCENES):
        baseline = renderer.render(index)
        variants = {
            "baseline": baseline,
            "time_plus_2s": renderer.render(index, time=14.0),
            "loud_audio": renderer.render(index, audio=(0.9, 0.8, 0.85, 0.95)),
            "silence": renderer.render(index, audio=(0, 0, 0, 0)),
        }
        variants.update({f"palette_{palette}": renderer.render(index, palette=k) for k, palette in enumerate(PALETTES)})
        variants.update({f"particle_{mode}": renderer.render(index, particle_mode=k) for k, mode in enumerate(PARTICLE_MODES)})
        variants.update({
            "min_controls": renderer.render(index, time=0, speed=0, symmetry=1, audio=(0, 0, 0, 0)),
            "max_controls": renderer.render(index, time=3600, speed=1, symmetry=16, audio=(1, 1, 1, 1), kick=1),
            "day_timestamp": renderer.render(index, time=86400, speed=1, symmetry=16),
        })
        stats = {}
        for key, frame in variants.items():
            # Explicit `particle_field` + `none` intentionally yields background.
            patterned = not (index == 2 and key == "particle_none")
            stats[key] = assert_valid_frame(frame, patterned=patterned)
            stats[key]["spatial_pattern_required"] = patterned
            stats[key]["mean_abs_delta_from_baseline"] = frame_delta(baseline, frame)
        for key in ("time_plus_2s", "loud_audio", "silence"):
            _require(stats[key]["mean_abs_delta_from_baseline"] > 1e-5, (name, key))
        for palette in PALETTES[1:]:
            _require(stats[f"palette_{palette}"]["mean_abs_delta_from_baseline"] > 1e-5, (name, palette))
        if index in (2, 3):
            for mode in PARTICLE_MODES[1:]:
                _require(stats[f"particle_{mode}"]["mean_abs_delta_from_baseline"] > 1e-5, (name, mode))
        # Record unchanged channels honestly; they are not used in this shader.
        sensitivity = {}
        for channel, changes in {
            "bass": {"audio": (0.9, 0.45, 0.25, 0.4)},
            "mid": {"audio": (0.35, 0.9, 0.25, 0.4)},
            "high": {"audio": (0.35, 0.45, 0.9, 0.4)},
            "energy": {"audio": (0.35, 0.45, 0.25, 0.9)},
            "kick": {"kick": 1},
            "camera_speed": {"speed": 0.9},
            "symmetry": {"symmetry": 16},
        }.items():
            frame = renderer.render(index, **changes)
            assert_valid_frame(frame)
            sensitivity[channel] = frame_delta(baseline, frame)
        rows.append({"scene": name, "cases": stats, "single_input_mean_abs_deltas": sensitivity})
        previews.append([variants[key] for key in ("baseline", "time_plus_2s", "loud_audio")])
    # Assert the five branch results are not accidental duplicates.
    for first in range(len(previews)):
        for second in range(first + 1, len(previews)):
            _require(frame_delta(previews[first][0], previews[second][0]) > 1e-5, "Duplicate scene branches")
    return rows, previews


def write_artifacts(output, renderer, rows, previews):
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "time_source": "Cloud host OS clock; not independently synchronized by this tool",
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "renderer": renderer.info,
        "resolution": [renderer.width, renderer.height],
        "framebuffer": "GL_RGBA32F, read as float32 before any PNG conversion",
        "shader": "td/shaders/visualizer.frag",
        "shader_sha256": hashlib.sha256(renderer.shader_bytes).hexdigest(),
        "shader_source_modified": False,
        "host_shim": "GLSL 330 core; normalized vUV; identity TDOutputSwizzle",
        "rendered_frame_count": sum(len(row["cases"]) + len(row["single_input_mean_abs_deltas"]) for row in rows),
        "passed": True,
        "checks": {
            "all_frames_finite_bounded_and_opaque": True,
            "spatially_patterned_frames": sum(
                sum(case["spatial_pattern_required"] for case in row["cases"].values())
                + len(row["single_input_mean_abs_deltas"]) for row in rows
            ),
            "intentional_background_only_frames": sum(
                sum(not case["spatial_pattern_required"] for case in row["cases"].values())
                for row in rows
            ),
            "intentional_background_case": "particle_field / particle_none",
            "every_scene_changes_with_time_and_audio": True,
            "all_five_scene_baselines_are_distinct": True,
        },
        "scenes": rows,
        "limitations": [
            "No TouchDesigner runtime, Metal compiler, saved .toe or Mac GPU was used.",
            "No GPU/60 fps/long-duration performance claim; llvmpipe is software rendering.",
            "No audio device, OSC wiring, transitions, feedback or external projectM input validation.",
            "projectm_blend here is the shader's native analytic fallback branch.",
            "Mid and kick uniforms are wired by the builder but unused by this fragment shader.",
            "particle_field with particle mode none intentionally yields a uniform dark background.",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    sheet = np.concatenate([np.concatenate(row, axis=1) for row in previews], axis=0)
    (output / "contact-sheet.png").write_bytes(png_bytes(sheet))
    width, height = renderer.width, renderer.height
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * 3}" height="{(height + 48) * 5 + 40}" viewBox="0 0 {width * 3} {(height + 48) * 5 + 40}">', '<rect width="100%" height="100%" fill="#10131b"/>', '<g fill="#eef2fa" font-family="sans-serif" font-size="16">']
    for col, label in enumerate(("Baseline, t=12s", "Time only, t=14s", "Louder audio, t=12s")):
        svg.append(f'<text x="{col * width + 8}" y="24">{label}</text>')
    for row, name in enumerate(SCENES):
        y = 40 + row * (height + 48)
        svg.append(f'<text x="8" y="{y + 24}">{name}</text>')
        for col, frame in enumerate(previews[row]):
            encoded = base64.b64encode(png_bytes(frame)).decode()
            svg.append(f'<image x="{col * width}" y="{y + 40}" width="{width}" height="{height}" href="data:image/png;base64,{encoded}"/>')
        (output / f"{name}.png").write_bytes(png_bytes(previews[row][0]))
    svg.append("</g></svg>")
    (output / "contact-sheet.svg").write_text("\n".join(svg) + "\n")
    lines = [
        "# Cloud shader verification", "",
        f"Generated: {report['created_at_utc']}", "",
        "PASS: the repository's unmodified shared GLSL shader compiled and linked; all five scene branches rendered.",
        f"120 RGBA32F frames at {width} × {height} were finite, in [0, 1], and fully opaque. 119 had measurable spatial structure; one intentional exception, particle_field with particle_mode=none, was a uniform dark background.",
        "Every scene changed with time, audio and palette; all five baseline scenes were distinct.", "",
        "## Reproduce", "",
        "```sh", ".venv/bin/python tools/render_shaders.py",
        "AMV_TEST_EGL=1 .venv/bin/python -m pytest -q tests/test_cloud_shaders.py", "```", "",
        "Default pytest runs only the dependency-free helper checks; six real-render tests require the explicit EGL opt-in. Once opted in, missing EGL or compile failures fail the tests instead of being skipped.", "",
        "## Coverage", "",
        "Per scene: baseline, time +2 s, loud audio, silence, five palettes, five particle modes, minimum controls at t=0, maximum controls at t=3600, and t=86400. Seven additional single-input sensitivity renders vary bass, mid, high, energy, kick, camera speed and symmetry.",
        "The opt-in tests also render all five scenes at 161×91 (odd dimensions/center pixel), 128×128 (square aspect), and 1280×720 (the TD builder's configured resolution), and verify intentional compile failure/context recovery.", "",
        "## Contact sheet", "",
        "contact-sheet.svg is self-contained and labeled; contact-sheet.png contains the same 15 preview images without labels.",
        "Rows, top to bottom: fractal_temple, tunnel, particle_field, kaleido_mesh, projectm_blend.",
        "Columns, left to right: baseline t=12 s; same inputs at t=14 s; louder audio at t=12 s.",
        "Baseline audio=(0.35, 0.45, 0.25, 0.4); louder audio=(0.9, 0.8, 0.85, 0.95). Palette=violet_cyan, particle_mode=spiral, speed=0.4, symmetry=8, kick=0.", "",
        "## Environment", "",
        f"- Python: {platform.python_version()}", f"- numpy: {np.__version__}",
        f"- EGL: {renderer.info['egl_version']} / {renderer.info['egl_vendor']}",
        f"- OpenGL: {renderer.info['gl_version']}", f"- GLSL: {renderer.info['glsl_version']}",
        f"- Renderer: {renderer.info['gl_renderer']}",
        f"- Shader SHA-256: {report['shader_sha256']}",
        "- No new Python packages were installed; EGL/GL are installed system libraries accessed with ctypes.", "",
        "## Measured control scope and limits", "",
        "Mid and kick changes produced exactly zero image delta for all five branches: the fragment shader does not reference those components. Camera speed does not affect fractal_temple; high does not affect tunnel; symmetry does not affect particle_field. This documents current behavior rather than changing it. Kick can still affect scene/control transitions elsewhere in TD.", "",
    ]
    lines.extend("- " + limitation for limitation in report["limitations"])
    lines += ["", "report.json contains raw float statistics, per-case image deltas, versions and source hash. PNG previews are quantized only after float validation.", ""]
    (output / "README.md").write_text("\n".join(lines))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/cloud-verification/shaders")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    args = parser.parse_args(argv)
    with ShaderRenderer(args.width, args.height) as renderer:
        rows, previews = verify_matrix(renderer)
        report = write_artifacts(args.output, renderer, rows, previews)
    print(f"PASS: {len(rows)} scene branches, {report['rendered_frame_count']} float32 frames")
    print(f"Renderer: {report['renderer']['gl_renderer']}")
    print(f"Artifacts: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
