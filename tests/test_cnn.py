"""ArtCNN's mpv shaders, read into compute passes (framegen/cnn.py). Running them needs OpenGL: see the module."""
import pytest

from framegen.cnn import MODELS, SHADER_DIR, _PRECISIONS, compute_source, parse


@pytest.mark.parametrize("model", sorted(MODELS))
def test_models_read_as_compute_passes(model):
    passes = parse((SHADER_DIR / MODELS[model]).read_text(encoding="utf-8"))
    assert passes[0]["binds"] == ["LUMA"] and passes[-1]["save"] is None
    saved = {"LUMA"}
    for p in passes:  # each pass reads only what the ones before it wrote
        assert set(p["binds"]) <= saved
        saved.add(p["save"])
    assert passes[-1]["scale"] == (2.0, 2.0)  # luma doubled
    for header in (h for _, h in _PRECISIONS):
        src = compute_source(passes[1], header)
        assert src.startswith("#version 450\n") and "#ifdef" not in src
        assert src.count("texelFetch(") == 1  # only inside fetch0: every read outside the texture is 0


def test_unsupported_shaders_are_refused():
    with pytest.raises(ValueError):
        parse("//!DESC x\n//!HOOK MAIN\n//!COMPUTE 8 8 8 8\nvoid hook() {}\n")
    with pytest.raises(ValueError):
        parse("//!DESC x\n//!HOOK LUMA\n//!BIND LUMA\nvec4 hook() { return vec4(0); }\n")
