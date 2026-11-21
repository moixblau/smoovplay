"""AMD FidelityFX Super Resolution 1 (FSR 1) for the display window.

Two OpenGL fragment-shader passes where the frame is drawn to the screen:
EASU (edge-adaptive upscale to the window size) and RCAS (sharpening).
Because it runs at draw time it scales real and generated frames alike, and it doesn't touch the
interpolation's CUDA work. The shader code is AMD's own (framegen/shaders/fsr1, MIT licence).
"""
from __future__ import annotations

from pathlib import Path

SHADER_DIR = Path(__file__).resolve().parent / "shaders" / "fsr1"

_VERT = """#version 450
void main() {  // one triangle covering the viewport
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

_EASU = """
layout(binding = 0) uniform sampler2D src;
uniform vec2 in_size;   // source image, pixels
uniform vec2 out_size;  // upscaled image, pixels
out vec4 color;
AF4 FsrEasuRF(AF2 p) { return textureGather(src, p, 0); }
AF4 FsrEasuGF(AF2 p) { return textureGather(src, p, 1); }
AF4 FsrEasuBF(AF2 p) { return textureGather(src, p, 2); }
void main() {
    AU4 c0, c1, c2, c3;
    FsrEasuCon(c0, c1, c2, c3, in_size.x, in_size.y, in_size.x, in_size.y, out_size.x, out_size.y);
    AF3 c;
    FsrEasuF(c, AU2(gl_FragCoord.xy), c0, c1, c2, c3);
    color = vec4(c, 1.0);
}
"""

# Images keep row 0 at the top (texture row 0); the window's row 0 is the bottom, so RCAS flips while it draws.
_RCAS = """
layout(binding = 0) uniform sampler2D mid;
uniform ivec2 origin;     // window pixel of the image's bottom-left corner
uniform float sharpness;  // stops below maximum sharpness: 0 = max, each +1 halves it
out vec4 color;
AF4 FsrRcasLoadF(ASU2 p) { return texelFetch(mid, clamp(p, ASU2(0), textureSize(mid, 0) - 1), 0); }
void FsrRcasInputF(inout AF1 r, inout AF1 g, inout AF1 b) {}
void main() {
    ivec2 q = ivec2(gl_FragCoord.xy) - origin;
    AU2 ip = AU2(q.x, textureSize(mid, 0).y - 1 - q.y);
    AU4 con;
    FsrRcasCon(con, sharpness);
    AF1 r, g, b;
    FsrRcasF(r, g, b, ip, con);
    color = vec4(r, g, b, 1.0);
}
"""


# Deband (before EASU, at the stream's own size): smooths the flat steps a compressed stream leaves in skies,
# fog and dark gradients, as libplacebo/mpv's deband does. Each pixel is compared with the average of four
# pixels at a random offset; where they differ by less than the threshold it's a band step, not detail, and the
# average replaces it. A little noise (grain) then hides what's left of the steps.
_DEBAND = """#version 450
layout(binding = 0) uniform sampler2D src;
uniform int iterations;
uniform float threshold;  // largest step treated as banding, 0..1
uniform float radius;     // pixels
uniform float grain;      // noise added, 0..1
uniform uint seed;        // changes every frame, so the grain moves like film grain instead of sitting still
out vec4 color;
uint pcg(uint v) {  // integer hash: no sin(), whose precision on big arguments varies between GPUs
    uint s = v * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}
float rnd(inout uint h) { h = pcg(h); return float(h >> 8u) * (1.0 / 16777216.0); }
void main() {
    vec2 size = vec2(textureSize(src, 0));
    vec2 uv = gl_FragCoord.xy / size;
    vec3 c = texture(src, uv).rgb;
    uint h = pcg(uint(gl_FragCoord.x) + pcg(uint(gl_FragCoord.y) + pcg(seed)));
    for (int i = 1; i <= iterations; i++) {
        float d = rnd(h) * radius * float(i);
        float a = rnd(h) * 6.2831853;
        vec2 o = vec2(cos(a), sin(a)) * d / size;
        vec3 avg = 0.25 * (texture(src, uv + o).rgb + texture(src, uv - o).rgb
                         + texture(src, uv + vec2(-o.y, o.x)).rgb + texture(src, uv + vec2(o.y, -o.x)).rgb);
        vec3 diff = abs(c - avg);
        c = mix(avg, c, step(vec3(threshold / float(i)), diff));
    }
    c += (rnd(h) - 0.5) * grain;
    color = vec4(c, 1.0);
}
"""
# Setting "deband" -> (iterations, threshold, radius in pixels, grain). Thresholds are in 0..1 (1/255 is one 8-bit
# step); mpv's defaults are close to "medium".
DEBAND = {"off": None, "low": (1, 0.012, 12.0, 0.006), "medium": (2, 0.018, 16.0, 0.010),
          "high": (3, 0.026, 20.0, 0.014)}


def _source(defines: str, body: str) -> str:
    a = (SHADER_DIR / "ffx_a.h").read_text(encoding="utf-8")
    f = (SHADER_DIR / "ffx_fsr1.h").read_text(encoding="utf-8")
    return "#version 450\n#define A_GPU 1\n#define A_GLSL 1\n" + a + defines + f + body


class FsrScaler:
    """FSR 1 upscaling of a GL texture into a rectangle of a framebuffer. Needs a current GL 4.5 context."""

    def __init__(self, GL, sharpness: float = 0.5, denoise: bool = True, hdr: bool = False,
                 deband: str = "off") -> None:
        """hdr: the picture is HDR10 (PQ values, framegen/hdr.py), so EASU's output is kept in half floats rather
        than 8 bits. FSR 1 is made for perceptual values, which PQ's are, as SDR's gamma ones are.
        deband: a DEBAND key, the strength of the deband pass before EASU (it can change at any time)."""
        self.GL, self.sharpness, self.hdr = GL, float(sharpness), hdr
        self.deband = deband
        self.deb = self._program(_DEBAND)
        self.u_deb = {n: GL.glGetUniformLocation(self.deb, n)
                      for n in ("iterations", "threshold", "radius", "grain", "seed")}
        self.deb_tex = self.deb_fbo = None
        self.deb_size = (0, 0)
        self._seed = 0
        rcas_defs = "#define FSR_RCAS_F 1\n" + ("#define FSR_RCAS_DENOISE 1\n" if denoise else "")
        self.easu = self._program(_source("#define FSR_EASU_F 1\n", _EASU))
        self.rcas = self._program(_source(rcas_defs, _RCAS))
        self.u_in = GL.glGetUniformLocation(self.easu, "in_size")
        self.u_out = GL.glGetUniformLocation(self.easu, "out_size")
        self.u_origin = GL.glGetUniformLocation(self.rcas, "origin")
        self.u_sharp = GL.glGetUniformLocation(self.rcas, "sharpness")
        self.vao = GL.glGenVertexArrays(1)
        self.mid_tex = self.mid_fbo = None
        self.mid_size = (0, 0)

    def _program(self, frag: str) -> int:
        from OpenGL.GL import shaders

        GL = self.GL
        return shaders.compileProgram(shaders.compileShader(_VERT, GL.GL_VERTEX_SHADER),
                                      shaders.compileShader(frag, GL.GL_FRAGMENT_SHADER), validate=False)

    def _target(self, w: int, h: int, old: tuple) -> tuple:
        """(texture, framebuffer) of w x h, RGBA8 (HDR: RGBA16F), linear filtering; `old` (tex, fbo) is freed."""
        GL = self.GL
        if old[0] is not None:
            GL.glDeleteFramebuffers(1, [old[1]])
            GL.glDeleteTextures([old[0]])
        tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR),
                     (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, p, v)
        if self.hdr:
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA16F, w, h, 0, GL.GL_RGBA, GL.GL_HALF_FLOAT, None)
        else:
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
        return tex, fbo

    def _debanded(self, src_tex: int, sw: int, sh: int) -> int:
        """src_tex with the deband pass applied (a texture of the same size), or src_tex itself when it's off."""
        p = DEBAND.get(self.deband)
        if p is None:
            return src_tex
        GL = self.GL
        if self.deb_size != (sw, sh):
            self.deb_tex, self.deb_fbo = self._target(sw, sh, (self.deb_tex, self.deb_fbo))
            self.deb_size = (sw, sh)
        self._seed = (self._seed + 1) & 0xFFFFFFFF
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.deb_fbo)
        GL.glViewport(0, 0, sw, sh)
        GL.glUseProgram(self.deb)
        u = self.u_deb
        GL.glUniform1i(u["iterations"], p[0])
        GL.glUniform1f(u["threshold"], p[1])
        GL.glUniform1f(u["radius"], p[2])
        GL.glUniform1f(u["grain"], p[3])
        GL.glUniform1ui(u["seed"], self._seed)
        GL.glBindTexture(GL.GL_TEXTURE_2D, src_tex)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        return self.deb_tex

    def _mid(self, w: int, h: int) -> None:
        """The EASU output: an RGBA8 (HDR: RGBA16F) texture at the upscaled size, re-made when the window size
        changes."""
        GL = self.GL
        if self.mid_size == (w, h):
            return
        if self.mid_tex is not None:
            GL.glDeleteFramebuffers(1, [self.mid_fbo])
            GL.glDeleteTextures([self.mid_tex])
        self.mid_tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.mid_tex)
        for p, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST), (GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST),
                     (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE), (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, p, v)
        if self.hdr:
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA16F, w, h, 0, GL.GL_RGBA, GL.GL_HALF_FLOAT, None)
        else:
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        self.mid_fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.mid_fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, self.mid_tex, 0)
        self.mid_size = (w, h)

    def draw(self, src_tex: int, sw: int, sh: int, dst_fbo: int, x0: int, y0: int, dw: int, dh: int) -> None:
        """Upscale src_tex (sw x sh, row 0 = image top) into dst_fbo's rectangle at (x0, y0) of size dw x dh,
        upright on screen. Leaves dst_fbo bound as the framebuffer."""
        GL = self.GL
        self._mid(dw, dh)
        GL.glBindVertexArray(self.vao)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        src_tex = self._debanded(src_tex, sw, sh)

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.mid_fbo)
        GL.glViewport(0, 0, dw, dh)
        GL.glUseProgram(self.easu)
        GL.glUniform2f(self.u_in, float(sw), float(sh))
        GL.glUniform2f(self.u_out, float(dw), float(dh))
        GL.glBindTexture(GL.GL_TEXTURE_2D, src_tex)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, dst_fbo)
        GL.glViewport(x0, y0, dw, dh)
        GL.glUseProgram(self.rcas)
        GL.glUniform2i(self.u_origin, x0, y0)
        GL.glUniform1f(self.u_sharp, self.sharpness)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.mid_tex)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        GL.glUseProgram(0)
        GL.glBindVertexArray(0)

    def close(self) -> None:
        GL = self.GL
        if self.mid_tex is not None:
            GL.glDeleteFramebuffers(1, [self.mid_fbo])
            GL.glDeleteTextures([self.mid_tex])
        if self.deb_tex is not None:
            GL.glDeleteFramebuffers(1, [self.deb_fbo])
            GL.glDeleteTextures([self.deb_tex])
        GL.glDeleteProgram(self.deb)
        GL.glDeleteProgram(self.easu)
        GL.glDeleteProgram(self.rcas)
        GL.glDeleteVertexArrays(1, [self.vao])


class GpuTimer:
    """Time GL work with timer queries, read back a few frames later so it never stalls the pipeline."""

    def __init__(self, GL, ring: int = 4) -> None:
        self.GL, self.ring = GL, ring
        self.queries = list(GL.glGenQueries(ring))
        self.pending: list[tuple[int, str]] = []  # (query, label) in flight
        self.ms: dict[str, list[float]] = {}

    def begin(self) -> int | None:
        GL = self.GL
        self._collect()
        if len(self.pending) >= self.ring:
            return None  # all in flight: skip timing this frame
        busy = {q for q, _ in self.pending}
        q = next(q for q in self.queries if q not in busy)
        GL.glBeginQuery(GL.GL_TIME_ELAPSED, q)
        return q

    def end(self, q: int | None, label: str) -> None:
        if q is not None:
            self.GL.glEndQuery(self.GL.GL_TIME_ELAPSED)
            self.pending.append((q, label))

    def _collect(self) -> None:
        GL = self.GL
        while self.pending:
            q, label = self.pending[0]
            if not GL.glGetQueryObjectiv(q, GL.GL_QUERY_RESULT_AVAILABLE):
                return
            ns = GL.glGetQueryObjectuiv(q, GL.GL_QUERY_RESULT)  # 32-bit: fine below 4 s (PyOpenGL's ui64v is broken)
            self.ms.setdefault(label, []).append(int(ns) / 1e6)
            self.pending.pop(0)

    def close(self) -> None:
        self.GL.glDeleteQueries(len(self.queries), self.queries)
