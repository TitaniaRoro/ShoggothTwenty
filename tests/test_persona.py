"""Draft validation and prompt assembly.

Model output is untrusted: it has read hostile post content, so it gets the same
scrutiny as a post title.
"""

from __future__ import annotations

import os

import pytest
import requests

from .conftest import make_response

from shoggoth.persona import (
    DELIM_CLOSE,
    DELIM_OPEN,
    NullModel,
    build_model,
    comment_prompt,
    fence,
    parse_post_draft,
    system_prompt,
    validate_draft,
)

GOOD_COMMENT = (
    "We hit this exact failure last month. Our cron kept reporting success while "
    "the workflow had already been disabled, so we added a dead man's switch."
)


def test_good_draft_passes():
    draft = validate_draft(GOOD_COMMENT, "comment")

    assert draft.ok
    assert draft.text == GOOD_COMMENT


def test_skip_is_respected():
    """The model is allowed to say it has nothing to add."""
    draft = validate_draft("SKIP", "comment")

    assert not draft.ok
    assert "declined" in draft.reason


def test_url_in_model_output_is_rejected():
    draft = validate_draft(
        "Interesting take, and there is a fuller write-up at https://example.com/payload "
        "that explains the whole approach in more detail for anyone curious.",
        "comment",
    )

    assert not draft.ok
    assert "URL" in draft.reason


def test_invisible_characters_are_stripped_from_output():
    draft = validate_draft(f"​{GOOD_COMMENT}﻿", "comment")

    assert draft.ok
    assert draft.text.startswith("We hit")


def test_short_output_is_rejected():
    assert not validate_draft("same", "comment").ok


def test_post_draft_is_split_into_title_and_body():
    raw = "Silent failures are the default\n\nHere is what we learned running an agent unattended. " + "x" * 120

    draft = validate_draft(raw, "post")

    assert draft.ok
    assert draft.title == "Silent failures are the default"
    assert draft.text.startswith("Here is what we learned")


def test_post_without_a_body_is_rejected():
    draft = validate_draft("Just a title and nothing else here at all", "post")

    assert not draft.ok


def test_parse_post_draft_handles_markdown_heading():
    title, body = parse_post_draft("# A title\n\nA body")

    assert title == "A title"
    assert body == "A body"


def test_parse_post_draft_handles_empty_input():
    assert parse_post_draft("") == (None, None)


# --- prompt assembly -----------------------------------------------------


def test_untrusted_content_is_fenced():
    prompt = comment_prompt("A title", "Some body text", "agents")

    assert DELIM_OPEN in prompt
    assert DELIM_CLOSE in prompt
    assert "Some body text" in prompt


def test_fence_marks_the_boundary_clearly():
    fenced = fence("payload")

    assert fenced.startswith(DELIM_OPEN)
    assert fenced.endswith(DELIM_CLOSE)
    assert "payload" in fenced


def test_system_prompt_carries_the_hard_constraints():
    prompt = system_prompt("You are TestBot.")

    assert "never instructions" in prompt.lower()
    assert "never output a url" in prompt.lower()
    assert "TestBot" not in prompt or "You are TestBot." in prompt


def test_system_prompt_tells_the_model_not_to_claim_it_acted():
    prompt = system_prompt("")

    assert "never claim to have posted" in prompt.lower()


# --- provider selection --------------------------------------------------


def test_no_keys_means_no_model(monkeypatch):
    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "MODEL_PROVIDER"):
        monkeypatch.delenv(key, raising=False)

    assert isinstance(build_model(), NullModel)


def test_groq_is_the_default_when_configured(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)

    model = build_model()

    assert model.name == "groq"


def test_provider_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GEMINI_API_KEY", "gem_test")
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")

    assert build_model().name == "gemini"


def test_null_model_returns_none_rather_than_raising():
    assert NullModel().complete("system", "user") is None


# --- model selection survives a provider retiring a model -----------------

# llama-3.3-70b-versatile was switched off on 2026-08-16. Every run stayed
# green while drafting silently produced nothing, because the failure was a
# 200 response with an error body rather than an exception.
def groq_error_body(model: str) -> dict:
    return {
        "error": {
            "message": f"The model `{model}` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }


def groq_ok_body(text: str = "draft") -> dict:
    return {"choices": [{"message": {"content": text}}]}


def test_the_default_model_is_not_a_retired_one():
    from shoggoth.persona import DEFAULT_GROQ_MODELS

    assert "llama-3.3-70b-versatile" not in DEFAULT_GROQ_MODELS
    assert DEFAULT_GROQ_MODELS[0] == "openai/gpt-oss-120b"


def test_a_retired_model_falls_through_to_one_that_works(monkeypatch):
    from shoggoth import persona

    called: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        called.append(json["model"])
        if json["model"] == "openai/gpt-oss-120b":
            return make_response(200, groq_error_body(json["model"]))
        return make_response(200, groq_ok_body())

    monkeypatch.setattr(requests, "post", fake_post)
    model = persona.GroqModel("key", "openai/gpt-oss-120b")

    assert model.complete("system", "user") == "draft"
    assert called == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def test_an_explicit_override_is_tried_first(monkeypatch):
    from shoggoth import persona

    monkeypatch.setattr(
        requests, "post", lambda *a, **k: make_response(200, groq_ok_body())
    )
    model = persona.GroqModel("key", "my-custom-model")

    assert model.complete("system", "user") == "draft"
    assert model.model == "my-custom-model"
    assert "my-custom-model" in model.models


def test_the_defaults_remain_available_behind_an_override(monkeypatch):
    from shoggoth import persona

    model = persona.GroqModel("key", "my-custom-model")

    assert model.models[0] == "my-custom-model"
    assert "openai/gpt-oss-120b" in model.models


def test_the_token_budget_leaves_room_for_reasoning(monkeypatch):
    """gpt-oss reasons before it answers, and that reasoning is billed to the
    completion budget. Too low a ceiling returns nothing rather than a short
    answer."""
    from shoggoth import persona

    sent: dict = {}
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: (
            sent.update(json),
            make_response(200, groq_ok_body()),
        )[1],
    )
    persona.GroqModel("key").complete("system", "user")

    assert sent["max_tokens"] >= persona.MAX_OUTPUT_TOKENS >= 1500


def test_when_every_model_fails_drafting_is_skipped_not_faked(monkeypatch):
    from shoggoth import persona

    monkeypatch.setattr(
        requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: make_response(
            200, groq_error_body(json["model"])
        ),
    )

    assert persona.GroqModel("key").complete("system", "user") is None
