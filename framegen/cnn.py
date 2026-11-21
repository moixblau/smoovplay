"""ArtCNN upscaling in OpenGL: a small convolutional network that doubles the picture's luma, run as compute shaders.

ArtCNN (MIT, framegen/shaders/artcnn) ships its models as mpv "user shaders": a list of compute passes, each with
//! lines saying which textures it reads (BIND), the one it writes (SAVE) and its size (WIDTH/HEIGHT). This module
runs those passes itself, so no mpv or libplacebo is needed:

  1. luma (BT.709 Y') of the stream frame, at its own size (after the deband pass, which FsrScaler owns);
  2. the network's passes, which end in that luma at twice the size, sharper and with less compression noise
     (the "DS" models are trained to denoise and sharpen);
  3. that luma brought down to the window's size and put back together with the frame's colour, which stays a
     plain bilinear enlargement (the eye sees detail in luma, not in colour);
  4. FSR 1's RCAS, a light finishing sharpen, which also flips the picture upright into the window.

The network only adds work when the window is bigger than the stream (like FSR 1); at 1080p -> 1440p the luma
is doubled to 2160p and brought down to 1440p in step 3.
"""
from __future__ import annotations

import re
from pathlib import Path

from .fsr import FsrScaler

SHADER_DIR = Path(__file__).resolve().parent / "shaders" / "artcnn"
MODELS = {"light": "ArtCNN_C4F16_DS.glsl", "strong": "ArtCNN_C4F32_DS.glsl"}

# Half floats where the driver has them (twice the arithmetic rate on Radeon cards), else 32-bit floats.
_PRECISIONS = (
    ("fp16", "#extension GL_EXT_shader_explicit_arithmetic_types_float16 : require\n"
             "#define V4 f16vec4\n#define M4 f16mat4\n#define F float16_t\n"),
    ("fp16", "#extension GL_AMD_gpu_shader_half_float : require\n"
             "#define V4 f16vec4\n#define M4 f16mat4\n#define F float16_t\n"),
    ("fp32", "#define V4 vec4\n#define M4 mat4\n#define F float\n"),
)
# The models' own precision switch, replaced by the header above (an #extension line can't follow declarations).
_THEIR_PRECISION = re.compile(r"#extension GL_EXT_shader_explicit_arithmetic_types_float16 : enable\n"
                              r"#ifdef .*?#endif\n", re.S)

# What mpv gives a pass, for each texture it binds. texelFetch outside the texture reads 0, as the network's
# zero padding did in training (a plain texelFetch there is undefined in OpenGL).
_FETCH0 = """vec4 fetch0(sampler2D s, ivec2 p, int lod) {
    return all(greaterThanEqual(p, ivec2(0))) && all(lessThan(p, textureSize(s, 0))) ? texelFetch(s, p, 0)
                                                                                       : vec4(0.0);
}
"""
_BIND = """layout(binding = {i}) uniform sampler2D {n}_raw;
#define {n}_mul 1.0
#define {n}_size vec2(textureSize({n}_raw, 0))
#define {n}_pt (1.0 / {n}_size)
#define {n}_pos ((vec2(gl_GlobalInvocationID.xy) + 0.5) / vec2(imageSize(out_image)))
#define {n}_tex(p) texture({n}_raw, p)
"""

_LUMA = """#version 450
layout(binding = 0) uniform sampler2D src;
out vec4 color;
void main() {
    color = vec4(dot(texelFetch(src, ivec2(gl_FragCoord.xy), 0).rgb, vec3(0.2126, 0.7152, 0.0722)));
}
"""
# Step 3, into FSR's mid texture (row 0 = image top, as EASU writes it). The doubled luma is usually bigger than
# the window (2160 -> 1440 rows), so it's sampled at four points a quarter of a window pixel apart to keep fine
# lines from flickering; the frame's colour comes from a bilinear sample of the frame. Moving R, G and B by the
# same amount changes Y' and leaves Cb/Cr (B - Y', R - Y') as they were.
_COMBINE = """#version 450
layout(binding = 0) uniform sampler2D src;
layout(binding = 1) uniform sampler2D luma2;
uniform vec2 out_size;
out vec4 color;
void main() {
    vec2 uv = gl_FragCoord.xy / out_size, d = 0.25 / out_size;
    float y = 0.25 * (texture(luma2, uv + vec2(-d.x, -d.y)).x + texture(luma2, uv + vec2(d.x, -d.y)).x
                    + texture(luma2, uv + vec2(-d.x, d.y)).x + texture(luma2, uv + vec2(d.x, d.y)).x);
    vec3 c = texture(src, uv).rgb;
    color = vec4(clamp(c + (y - dot(c, vec3(0.2126, 0.7152, 0.0722))), 0.0, 1.0), 1.0);
}
"""


def parse(text: str) -> list[dict]:
    """The passes of an mpv user shader: [{binds, save, scale (w, h), block (w, h), threads (w, h), body}]."""
    passes: list[dict] = []
    for chunk in text.split("//!DESC")[1:]:
        p: dict = {"binds": [], "save": None}
        body = []
        for line in chunk.splitlines(keepends=True)[1:]:
            if not line.startswith("//!"):
                body.append(line)
                continue
            key, _, val = line[3:].strip().partition(" ")
            args = val.split()
            if key == "BIND":
                p["binds"].append(args[0])
            elif key == "SAVE":
                p["save"] = args[0]
            elif key in ("WIDTH", "HEIGHT"):
                if len(args) != 3 or args[0] != f"LUMA.{key[0].lower()}" or args[2] != "*":
                    raise ValueError(f"unsupported {key}: {val}")
                p[key] = float(args[1])
            elif key == "COMPUTE":
                bw, bh, tw, th = (int(a) for a in args)
                p["block"], p["threads"] = (bw, bh), (tw, th)
            elif key == "HOOK" and args[0] != "LUMA":
                raise ValueError(f"unsupported HOOK: {val}")
        if "block" not in p:
            raise ValueError("only compute passes are supported")
        p["scale"] = (p.pop("WIDTH", 1.0), p.pop("HEIGHT", 1.0))
        p["body"] = "".join(body)
        passes.append(p)
    if not passes or passes[-1]["save"] is not None:
        raise ValueError("the last pass should write the hooked luma (no SAVE)")
    return passes


def compute_source(p: dict, precision: str) -> str:
    """GLSL for one pass: the mpv environment it expects, then its own code."""
    tw, th = p["threads"]
    head = (f"#version 450\n{precision}layout(local_size_x = {tw}, local_size_y = {th}) in;\n"
            "layout(rgba16f, binding = 0) writeonly uniform image2D out_image;\n" + _FETCH0)
    head += "".join(_BIND.format(i=i, n=n) for i, n in enumerate(p["binds"]))
    body = _THEIR_PRECISION.sub("", p["body"]).replace("texelFetch(", "fetch0(")
    return head + body + "void main() { hook(); }\n"


class CnnScaler(FsrScaler):
    """ArtCNN upscaling of a GL texture into a rectangle of a framebuffer, with FsrScaler's deband before it and
    its RCAS after it. Same draw() as FsrScaler. Needs a current GL 4.5 context."""

    def __init__(self, GL, sharpness: float = 0.5, hdr: bool = False, deband: str = "off",
                 model: str = "light") -> None:
        super().__init__(GL, sharpness, hdr=hdr, deband=deband)
        from OpenGL.GL import shaders

        self.model = model
        self.passes = parse((SHADER_DIR / MODELS[model]).read_text(encoding="utf-8"))
        errors = []
        for self.precision, header in _PRECISIONS:
            try:
                self.progs = [shaders.compileProgram(shaders.compileShader(compute_source(p, header),
                                                                           GL.GL_COMPUTE_SHADER), validate=False)
                              for p in self.passes]
                break
            except Exception as e:  # noqa: BLE001 - this precision isn't available: try the next
                errors.append(str(e)[:300])
        else:
            raise RuntimeError("ArtCNN shaders didn't compile: " + " | ".join(errors))
        self.luma = self._program(_LUMA)
        self.combine = self._program(_COMBINE)
        self.u_out_size = GL.glGetUniformLocation(self.combine, "out_size")
        self.linear, self.nearest = GL.glGenSamplers(2)
        for s, f in ((self.linear, GL.GL_LINEAR), (self.nearest, GL.GL_NEAREST)):
            for p, v in ((GL.GL_TEXTURE_MIN_FILTER, f), (GL.GL_TEXTURE_MAG_FILTER, f),
                         (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
                GL.glSamplerParameteri(s, p, v)
        self.cnn_size = (0, 0)
        self.cnn_tex: dict[str, int] = {}
        self.luma_fbo = None

    def _float_tex(self, w: int, h: int) -> int:
        GL = self.GL
        tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexStorage2D(GL.GL_TEXTURE_2D, 1, GL.GL_RGBA16F, w, h)
        return tex

    def _free_cnn(self) -> None:
        GL = self.GL
        if self.cnn_tex:
            GL.glDeleteTextures(list(self.cnn_tex.values()))
            GL.glDeleteFramebuffers(1, [self.luma_fbo])
        self.cnn_tex, self.luma_fbo = {}, None

    def _cnn_targets(self, sw: int, sh: int) -> None:
        """The textures the passes read and write, re-made when the stream's size changes."""
        if self.cnn_size == (sw, sh):
            return
        GL = self.GL
        self._free_cnn()
        self.cnn_tex["LUMA"] = self._float_tex(sw, sh)
        self.luma_fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.luma_fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D,
                                  self.cnn_tex["LUMA"], 0)
        for p in self.passes:
            name = p["save"] or "OUT"
            self.cnn_tex[name] = self._float_tex(round(sw * p["scale"][0]), round(sh * p["scale"][1]))
        self.cnn_size = (sw, sh)

    def _enlarge(self, src_tex: int, sw: int, sh: int, dw: int, dh: int) -> None:
        GL = self.GL
        self._cnn_targets(sw, sh)
        t = self.cnn_tex
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.luma_fbo)
        GL.glViewport(0, 0, sw, sh)
        GL.glUseProgram(self.luma)
        GL.glBindTexture(GL.GL_TEXTURE_2D, src_tex)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glMemoryBarrier(GL.GL_TEXTURE_FETCH_BARRIER_BIT)
        for p, prog in zip(self.passes, self.progs):
            out = p["save"] or "OUT"
            ow, oh = round(sw * p["scale"][0]), round(sh * p["scale"][1])
            GL.glUseProgram(prog)
            GL.glBindImageTexture(0, t[out], 0, GL.GL_FALSE, 0, GL.GL_WRITE_ONLY, GL.GL_RGBA16F)
            for i, name in enumerate(p["binds"]):
                GL.glActiveTexture(GL.GL_TEXTURE0 + i)
                GL.glBindTexture(GL.GL_TEXTURE_2D, t[name])
                GL.glBindSampler(i, self.nearest)
            bw, bh = p["block"]
            GL.glDispatchCompute(-(-ow // bw), -(-oh // bh), 1)
            GL.glMemoryBarrier(GL.GL_TEXTURE_FETCH_BARRIER_BIT | GL.GL_SHADER_IMAGE_ACCESS_BARRIER_BIT)
        for i in range(max(len(p["binds"]) for p in self.passes)):
            GL.glBindSampler(i, 0)

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.mid_fbo)
        GL.glViewport(0, 0, dw, dh)
        GL.glUseProgram(self.combine)
        GL.glUniform2f(self.u_out_size, float(dw), float(dh))
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, t["OUT"])
        GL.glBindSampler(1, self.linear)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, src_tex)
        GL.glBindSampler(0, self.linear)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindSampler(0, 0)
        GL.glBindSampler(1, 0)

    def _rcas_sharpness(self) -> float:
        return self.sharpness + 1.0  # the network already sharpens: RCAS one step softer than with FSR 1

    def close(self) -> None:
        GL = self.GL
        self._free_cnn()
        for prog in (*self.progs, self.luma, self.combine):
            GL.glDeleteProgram(prog)
        GL.glDeleteSamplers(2, [self.linear, self.nearest])
        super().close()
