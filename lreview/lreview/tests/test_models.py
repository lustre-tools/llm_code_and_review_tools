"""Tests for the model catalog: aliases, defaults, effort ladders."""

import pytest

from lreview.models import (
    CODEX_DEFAULT_MODEL,
    CODEX_MODELS,
    EFFORT_LEVELS,
    canonical_model,
    codex_catalog_lines,
    codex_model,
    model_efforts,
    validate_selection,
)


class TestCatalog:

    def test_families_present(self):
        families = {m.family for m in CODEX_MODELS}
        assert {"GPT-6", "GPT-5.6"} <= families
        slugs = {m.slug for m in CODEX_MODELS}
        assert {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra",
                "gpt-5.6-luna"} <= slugs

    def test_default_is_a_catalog_model(self):
        assert codex_model(CODEX_DEFAULT_MODEL) is not None

    def test_efforts_are_known_levels_and_include_the_default(self):
        for model in CODEX_MODELS:
            assert set(model.efforts) <= set(EFFORT_LEVELS)
            assert model.default_effort in model.efforts

    def test_aliases_are_unique(self):
        seen = set()
        for model in CODEX_MODELS:
            for name in (model.slug,) + model.aliases:
                assert name not in seen, f"duplicate name {name}"
                seen.add(name)

    def test_catalog_lines_cover_every_model(self):
        text = "\n".join(codex_catalog_lines())
        for model in CODEX_MODELS:
            assert model.slug in text
        assert "(default)" in text


class TestCanonicalModel:

    @pytest.mark.parametrize("alias,slug", [
        ("sol", "gpt-5.6-sol"),
        ("SOL", "gpt-5.6-sol"),
        ("terra", "gpt-5.6-terra"),
        ("luna", "gpt-5.6-luna"),
        ("astra", "gpt-6-astra"),
        ("gpt-6", "gpt-6-astra"),
        ("gpt-5.6", "gpt-5.6-terra"),
        ("spark", "gpt-5.3-codex-spark"),
        ("gpt-5.6-sol", "gpt-5.6-sol"),
    ])
    def test_aliases_expand(self, alias, slug):
        assert canonical_model("codex", alias) == slug

    def test_unknown_model_passes_through(self):
        assert canonical_model("codex", "gpt-7-nova") == "gpt-7-nova"

    def test_other_agents_are_untouched(self):
        # "sol" is a codex alias; claude models must not be rewritten
        assert canonical_model("claude", "sol") == "sol"
        assert canonical_model("claude", "opus") == "opus"
        assert canonical_model("codex", None) is None


class TestValidateSelection:

    def test_accepts_a_supported_pair(self):
        validate_selection("codex", "gpt-5.6-sol", "medium")
        validate_selection("codex", "gpt-6-astra", "ultra")
        validate_selection("claude", "opus", "xhigh")

    def test_alias_is_validated_like_its_slug(self):
        validate_selection("codex", "sol", "ultra")
        with pytest.raises(ValueError, match="gpt-5.6-luna"):
            validate_selection("codex", "luna", "ultra")

    def test_rejects_ultra_on_a_model_without_it(self):
        with pytest.raises(ValueError) as exc:
            validate_selection("codex", "gpt-5.6-luna", "ultra")
        assert "does not support --effort ultra" in str(exc.value)
        assert "low, medium, high, xhigh, max" in str(exc.value)

    def test_rejects_max_on_the_older_models(self):
        for slug in ("gpt-5.5", "gpt-5.3-codex-spark"):
            with pytest.raises(ValueError, match="does not support"):
                validate_selection("codex", slug, "max")

    def test_ultra_is_codex_only(self):
        with pytest.raises(ValueError) as exc:
            validate_selection("claude", "opus", "ultra")
        assert "codex-only" in str(exc.value)

    def test_unknown_model_is_not_effort_checked(self):
        validate_selection("codex", "gpt-7-nova", "ultra")
        validate_selection("codex", None, "ultra")

    def test_agents_without_effort_are_ignored(self):
        validate_selection("gemini", "gemini-3-pro", "ultra")
        validate_selection("opencode", "anthropic/opus", "max")

    def test_no_effort_is_always_fine(self):
        validate_selection("codex", "gpt-5.5", None)


class TestModelEfforts:

    def test_claude_ladder_has_no_ultra(self):
        assert "ultra" not in model_efforts("claude", "opus")

    def test_unknown_codex_model_has_no_known_ladder(self):
        assert model_efforts("codex", "gpt-7-nova") is None

    def test_other_agents_have_no_ladder(self):
        assert model_efforts("gemini", "gemini-3-pro") is None
