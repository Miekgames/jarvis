"""Inline tool calls emitted by local models.

Small models routinely write the tool call into the message content instead of
the structured channel. Left alone, that JSON is rendered to the user as the
assistant's answer.
"""
from __future__ import annotations

import json

from backend.ai.providers.ollama_provider import _tool_names, parse_inline_tool_call


def test_parses_name_parameters_shape():
    call = parse_inline_tool_call(
        '{"name": "search", "parameters": {"q": "capital of France"}}')
    assert call["name"] == "search"
    assert call["input"] == {"q": "capital of France"}
    assert call["exclusive"] is True


def test_parses_every_argument_key():
    assert parse_inline_tool_call('{"name": "a", "arguments": {"x": 1}}')["input"] == {"x": 1}
    assert parse_inline_tool_call('{"name": "a", "input": {"x": 2}}')["input"] == {"x": 2}
    assert parse_inline_tool_call('{"tool": "a", "parameters": {}}')["name"] == "a"
    assert parse_inline_tool_call('{"tool_name": "a", "args": {}}')["name"] == "a"


def test_argument_key_is_chosen_by_presence_not_by_order():
    """Picking a shape on its name key alone dropped the arguments entirely."""
    assert parse_inline_tool_call(
        '{"name": "a", "arguments": {"x": 9}}')["input"] == {"x": 9}


def test_parses_fenced_json():
    fenced = '```json\n{"name": "system_status", "arguments": {}}\n```'
    call = parse_inline_tool_call(fenced)
    assert call["name"] == "system_status"
    assert call["exclusive"] is True


def test_fenced_json_inside_prose_is_not_exclusive():
    prose = ('Here is how you call it:\n```json\n{"name": "a", "arguments": {}}\n```\n'
             'That is all.')
    call = parse_inline_tool_call(prose)
    assert call is not None and call["exclusive"] is False


def test_string_arguments_are_parsed():
    """Some models double-encode the arguments as a JSON string."""
    payload = '{"name": "a", "arguments": ' + json.dumps('{"x": 3}') + '}'
    assert parse_inline_tool_call(payload)["input"] == {"x": 3}


def test_prose_containing_json_is_left_alone():
    assert parse_inline_tool_call('Here is an example: {"name": "bob", "args": {}}') is None


def test_plain_answer_is_left_alone():
    assert parse_inline_tool_call("The capital of France is Paris.") is None


def test_object_without_arguments_is_data_not_a_call():
    assert parse_inline_tool_call('{"name": "Paris", "country": "France"}') is None
    assert parse_inline_tool_call('{"result": 42}') is None


def test_non_dict_arguments_are_rejected():
    assert parse_inline_tool_call('{"name": "a", "parameters": [1, 2]}') is None


def test_tool_names_reads_both_schema_shapes():
    assert _tool_names([{"function": {"name": "a"}}]) == {"a"}
    assert _tool_names([{"name": "b"}]) == {"b"}
    assert _tool_names([]) is None
    assert _tool_names(None) is None


# ======================================================================
# a call followed by more talking
# ======================================================================
def test_call_with_trailing_prose_is_still_a_call():
    """The reported failure: the model emitted the call then kept talking, and
    the whole thing was printed to the user as the answer."""
    call = parse_inline_tool_call('{"name": "search", "parameters": {"q": "x"}} whats next')
    assert call is not None
    assert call["name"] == "search"
    assert call["input"] == {"q": "x"}
    assert call["exclusive"] is True


def test_braces_inside_string_values_do_not_end_the_object():
    call = parse_inline_tool_call('{"name": "a", "parameters": {"text": "has } brace"}} more')
    assert call is not None
    assert call["input"] == {"text": "has } brace"}


def test_escaped_quotes_inside_values_are_handled():
    payload = '{"name": "a", "parameters": {"text": "say \\"hi\\""}} then'
    call = parse_inline_tool_call(payload)
    assert call is not None
    assert call["input"]["text"] == 'say "hi"'


def test_json_quoted_inside_prose_is_still_left_alone():
    assert parse_inline_tool_call(
        'Example: {"name":"a","parameters":{}} is the shape.') is None


def test_leading_object_that_is_not_a_call_is_left_alone():
    assert parse_inline_tool_call('{"result": 42} done') is None
