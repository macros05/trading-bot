"""Tests for the Gemini agent loop in telegram_listener.py.

The Gemini client is fully mocked — no real API calls. Covers:
  * tool-calling round followed by a text answer
  * hard cap of 4 tool rounds
  * friendly error message on API failure (spec-exact text)
  * graceful message when GEMINI_API_KEY is missing
  * conversation memory: history injected into contents, exchange saved
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai import types

import telegram_listener as tl


def _text_response(text: str):
    content = types.Content(role='model', parts=[types.Part(text=text)])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def _tool_call_response(name: str, args: dict | None = None):
    part = types.Part(
        function_call=types.FunctionCall(name=name, args=args or {})
    )
    content = types.Content(role='model', parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def _mock_client(side_effect):
    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(side_effect=side_effect)
            )
        )
    )
    return client


class TestAgentLoop(unittest.IsolatedAsyncioTestCase):

    async def test_tool_round_then_text_answer(self) -> None:
        client = _mock_client([
            _tool_call_response('get_status'),
            _text_response('El bot está pausado, sin posición abierta.'),
        ])
        with patch.object(tl.genai, 'Client', return_value=client), \
             patch.object(tl.telegram_ai_tools, 'execute_tool',
                          return_value={'paused': True}) as mock_exec:
            contents = [types.Content(role='user',
                                      parts=[types.Part(text='¿cómo va?')])]
            reply = await tl._agent_loop(contents)

        self.assertEqual(reply, 'El bot está pausado, sin posición abierta.')
        mock_exec.assert_called_once_with('get_status', {})
        self.assertEqual(client.aio.models.generate_content.call_count, 2)
        # tool result was fed back as a function_response part
        roles = [c.role for c in contents]
        self.assertEqual(roles, ['user', 'model', 'user'])
        fn_resp = contents[-1].parts[0].function_response
        self.assertEqual(fn_resp.name, 'get_status')

    async def test_max_4_tool_rounds(self) -> None:
        client = _mock_client(
            lambda **_: _tool_call_response('get_status')
        )
        with patch.object(tl.genai, 'Client', return_value=client), \
             patch.object(tl.telegram_ai_tools, 'execute_tool',
                          return_value={}):
            reply = await tl._agent_loop(
                [types.Content(role='user', parts=[types.Part(text='hola')])]
            )
        self.assertEqual(client.aio.models.generate_content.call_count, 4)
        self.assertEqual(reply, '')

    async def test_api_failure_returns_friendly_message(self) -> None:
        client = _mock_client(RuntimeError('503 UNAVAILABLE — internal stack trace'))
        with patch.object(tl.genai, 'Client', return_value=client), \
             patch.object(tl, '_GEMINI_KEY', 'fake-key'):
            reply = await tl._ai_answer('¿qué tal?')
        self.assertEqual(
            reply, '⚠️ IA temporalmente no disponible, prueba en unos segundos.')
        self.assertNotIn('503', reply)
        self.assertNotIn('stack trace', reply)

    async def test_missing_api_key_keeps_graceful_message(self) -> None:
        with patch.object(tl, '_GEMINI_KEY', ''):
            reply = await tl._ai_answer('hola')
        self.assertIn('GEMINI_API_KEY', reply)

    async def test_successful_answer_is_saved_to_memory(self) -> None:
        client = _mock_client([_text_response('Todo en orden.')])
        with patch.object(tl.genai, 'Client', return_value=client), \
             patch.object(tl, '_GEMINI_KEY', 'fake-key'), \
             patch.object(tl.telegram_chat_memory, 'load_exchanges',
                          return_value=[]), \
             patch.object(tl.telegram_chat_memory, 'save_exchange') as mock_save:
            reply = await tl._ai_answer('¿estado?')
        self.assertEqual(reply, 'Todo en orden.')
        mock_save.assert_called_once_with('¿estado?', 'Todo en orden.')

    async def test_failed_answer_is_not_saved_to_memory(self) -> None:
        client = _mock_client(RuntimeError('boom'))
        with patch.object(tl.genai, 'Client', return_value=client), \
             patch.object(tl, '_GEMINI_KEY', 'fake-key'), \
             patch.object(tl.telegram_chat_memory, 'save_exchange') as mock_save:
            await tl._ai_answer('¿estado?')
        mock_save.assert_not_called()

    async def test_history_is_injected_into_contents(self) -> None:
        history = [{'user': 'pregunta vieja', 'assistant': 'respuesta vieja'}]
        captured = {}

        async def capture(**kwargs):
            captured['contents'] = list(kwargs['contents'])
            return _text_response('ok')

        client = _mock_client(capture)
        with patch.object(tl.genai, 'Client', return_value=client), \
             patch.object(tl, '_GEMINI_KEY', 'fake-key'), \
             patch.object(tl.telegram_chat_memory, 'load_exchanges',
                          return_value=history), \
             patch.object(tl.telegram_chat_memory, 'save_exchange'):
            await tl._ai_answer('pregunta nueva')

        contents = captured['contents']
        self.assertEqual([c.role for c in contents], ['user', 'model', 'user'])
        self.assertEqual(contents[0].parts[0].text, 'pregunta vieja')
        self.assertEqual(contents[1].parts[0].text, 'respuesta vieja')
        self.assertEqual(contents[2].parts[0].text, 'pregunta nueva')


class TestSystemPromptAndConstants(unittest.TestCase):

    def test_round_cap_is_4(self) -> None:
        self.assertEqual(tl._MAX_TOOL_ROUNDS, 4)

    def test_friendly_error_text_is_spec_exact(self) -> None:
        self.assertEqual(
            tl._AI_UNAVAILABLE_MSG,
            '⚠️ IA temporalmente no disponible, prueba en unos segundos.')

    def test_prompt_describes_all_tools(self) -> None:
        import telegram_ai_tools
        for name in telegram_ai_tools.TOOLS:
            self.assertIn(name, tl._SYSTEM_PROMPT)

    def test_prompt_rules(self) -> None:
        prompt = tl._SYSTEM_PROMPT
        self.assertIn('@macrosAssistant', prompt)   # off-scope redirect
        self.assertIn('español', prompt.lower())    # answer in Spanish
        self.assertIn('inventes', prompt)           # statistical honesty


if __name__ == '__main__':
    unittest.main()
