from agent_commerce.core.llm.cache import CachingLLMClient
from agent_commerce.core.llm.fake import FakeLLMClient, text_response, tool_response
from agent_commerce.core.llm.types import Message, ToolChoice, ToolSpec


def test_identical_request_hits_cache_and_skips_the_wrapped_client(tmp_path) -> None:
    fake = FakeLLMClient([text_response("first")])
    cached = CachingLLMClient(fake, cache_dir=tmp_path)

    r1 = cached.complete(system="sys", messages=[Message(role="user", content="hi")])
    r2 = cached.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert r1.text == "first"
    assert r2.text == "first"
    assert r1.cached is False
    assert r2.cached is True
    assert len(fake.calls) == 1  # second call never reached the wrapped client


def test_different_message_content_does_not_hit_cache(tmp_path) -> None:
    fake = FakeLLMClient([text_response("a"), text_response("b")])
    cached = CachingLLMClient(fake, cache_dir=tmp_path)

    r1 = cached.complete(system="sys", messages=[Message(role="user", content="one")])
    r2 = cached.complete(system="sys", messages=[Message(role="user", content="two")])

    assert (r1.text, r2.text) == ("a", "b")
    assert len(fake.calls) == 2


def test_different_model_does_not_hit_cache(tmp_path) -> None:
    fake_a = FakeLLMClient([text_response("a")], model="model-a")
    fake_b = FakeLLMClient([text_response("b")], model="model-b")
    cached_a = CachingLLMClient(fake_a, cache_dir=tmp_path)
    cached_b = CachingLLMClient(fake_b, cache_dir=tmp_path)

    r1 = cached_a.complete(system="sys", messages=[Message(role="user", content="hi")])
    r2 = cached_b.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert (r1.text, r2.text) == ("a", "b")


def test_different_provider_does_not_hit_cache_even_with_same_model_name(tmp_path) -> None:
    class _OtherFake(FakeLLMClient):
        provider = "other-provider"

    fake_a = FakeLLMClient([text_response("a")], model="shared-model-name")
    fake_b = _OtherFake([text_response("b")], model="shared-model-name")
    cached_a = CachingLLMClient(fake_a, cache_dir=tmp_path)
    cached_b = CachingLLMClient(fake_b, cache_dir=tmp_path)

    r1 = cached_a.complete(system="sys", messages=[Message(role="user", content="hi")])
    r2 = cached_b.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert (r1.text, r2.text) == ("a", "b")


def test_different_temperature_does_not_hit_cache(tmp_path) -> None:
    fake = FakeLLMClient([text_response("a"), text_response("b")])
    cached = CachingLLMClient(fake, cache_dir=tmp_path)

    r1 = cached.complete(system="sys", messages=[Message(role="user", content="hi")], temperature=0.0)
    r2 = cached.complete(system="sys", messages=[Message(role="user", content="hi")], temperature=0.7)

    assert (r1.text, r2.text) == ("a", "b")


def test_different_max_tokens_does_not_hit_cache(tmp_path) -> None:
    # A different max_tokens can produce a genuinely different (truncated) response, so it
    # must be part of the cache key even though it's not billing-relevant like temperature.
    fake = FakeLLMClient([text_response("a"), text_response("b")])
    cached = CachingLLMClient(fake, cache_dir=tmp_path)

    r1 = cached.complete(system="sys", messages=[Message(role="user", content="hi")], max_tokens=100)
    r2 = cached.complete(system="sys", messages=[Message(role="user", content="hi")], max_tokens=500)

    assert (r1.text, r2.text) == ("a", "b")


def test_different_tools_does_not_hit_cache(tmp_path) -> None:
    fake = FakeLLMClient([text_response("a"), text_response("b")])
    cached = CachingLLMClient(fake, cache_dir=tmp_path)
    tool_a = ToolSpec(name="tool_a", description="", input_schema={})
    tool_b = ToolSpec(name="tool_b", description="", input_schema={})

    r1 = cached.complete(system="sys", messages=[Message(role="user", content="hi")], tools=[tool_a])
    r2 = cached.complete(system="sys", messages=[Message(role="user", content="hi")], tools=[tool_b])

    assert (r1.text, r2.text) == ("a", "b")


def test_different_tool_choice_does_not_hit_cache(tmp_path) -> None:
    fake = FakeLLMClient([text_response("a"), text_response("b")])
    cached = CachingLLMClient(fake, cache_dir=tmp_path)
    tool = ToolSpec(name="tool_a", description="", input_schema={})

    r1 = cached.complete(
        system="sys", messages=[Message(role="user", content="hi")], tools=[tool],
        tool_choice=ToolChoice(mode="auto"),
    )
    r2 = cached.complete(
        system="sys", messages=[Message(role="user", content="hi")], tools=[tool],
        tool_choice=ToolChoice(mode="specific", tool_name="tool_a"),
    )

    assert (r1.text, r2.text) == ("a", "b")


def test_tool_call_response_round_trips_through_cache(tmp_path) -> None:
    fake = FakeLLMClient([tool_response("my_tool", {"a": 1, "b": "x"})])
    cached = CachingLLMClient(fake, cache_dir=tmp_path)

    r1 = cached.complete(system="sys", messages=[Message(role="user", content="hi")])
    r2 = cached.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert r1.tool_calls[0].name == "my_tool"
    assert r2.tool_calls[0].arguments == {"a": 1, "b": "x"}
    assert r2.cached is True


def test_cache_persists_across_client_instances(tmp_path) -> None:
    fake1 = FakeLLMClient([text_response("only-once")])
    cached1 = CachingLLMClient(fake1, cache_dir=tmp_path)
    cached1.complete(system="sys", messages=[Message(role="user", content="hi")])

    fake2 = FakeLLMClient([])  # would raise if actually called
    cached2 = CachingLLMClient(fake2, cache_dir=tmp_path)
    response = cached2.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert response.text == "only-once"
    assert response.cached is True


def test_bypass_flag_forces_a_fresh_call_even_when_cached(tmp_path) -> None:
    fake = FakeLLMClient([text_response("first"), text_response("second")])
    cached_normal = CachingLLMClient(fake, cache_dir=tmp_path)
    cached_normal.complete(system="sys", messages=[Message(role="user", content="hi")])

    cached_bypass = CachingLLMClient(fake, cache_dir=tmp_path, bypass=True)
    response = cached_bypass.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert response.text == "second"
    assert response.cached is False
    assert len(fake.calls) == 2
