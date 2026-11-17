import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sage import config, llamacpp


class LlamaCppAdapterTests(unittest.TestCase):
    def test_request_uses_fixed_slot_and_openai_tool_messages(self):
        messages = [
            {"role": "user", "content": "Search the memory"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "search_memory", "arguments": {"query": "battery"}}},
            ]},
            {"role": "tool", "tool_name": "search_memory", "content": '{"matches": []}'},
        ]
        tool = {"type": "function", "function": {"name": "search_memory", "parameters": {"type": "object"}}}
        with patch.object(config, "LLAMA_CPP_MODEL", "sage-test"), patch.object(config, "LLAMA_CPP_SLOT", 0):
            body = llamacpp.request_body({
                "model": config.CODING_MODEL, "messages": messages, "stream": True,
                "tools": [tool], "think": False, "options": {"num_predict": 321, "num_ctx": 65536},
            })

        self.assertEqual(body["model"], "sage-test")
        self.assertEqual(body["id_slot"], 0)
        self.assertEqual(body["max_tokens"], 321)
        self.assertEqual(body["extra_params"], {"cache_prompt": True})
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["messages"][1]["tool_calls"][0]["function"]["arguments"], '{"query": "battery"}')
        self.assertEqual(body["messages"][2]["tool_call_id"], body["messages"][1]["tool_calls"][0]["id"])
        self.assertEqual(body["temperature"], 0.2)
        self.assertNotIn("num_ctx", body)

    def test_response_converts_openai_completion_and_timing_data(self):
        response = {
            "choices": [{"message": {
                "content": "Found it",
                "reasoning_content": "searching",
                "tool_calls": [{"id": "call-1", "type": "function",
                                "function": {"name": "search_memory", "arguments": '{"query":"battery"}'}}],
            }}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 12},
            "timings": {"cache_n": 60, "prompt_ms": 40, "predicted_ms": 20},
        }

        message = llamacpp.response_message(response)
        speed = llamacpp.speed_data(response)

        self.assertEqual(message["content"], "Found it")
        self.assertEqual(message["thinking"], "searching")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "search_memory")
        self.assertEqual(speed["prompt_eval_count"], 40)
        self.assertEqual(speed["eval_count"], 12)
        self.assertEqual(speed["eval_duration"], 20_000_000)

    def test_restore_only_contacts_server_when_snapshot_exists(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, "LLAMA_CPP_CACHE_DIR", Path(tmp)), \
                patch.object(llamacpp, "_cache_restore_attempted", False), \
                patch("sage.llamacpp.urllib.request.urlopen") as urlopen:
            llamacpp.restore_cache()
            urlopen.assert_not_called()

            (Path(tmp) / config.LLAMA_CPP_CACHE_FILE).write_bytes(b"snapshot")
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"n_restored": 1}'
            urlopen.return_value = response
            with patch.object(llamacpp, "_cache_restore_attempted", False):
                llamacpp.restore_cache()

            request = urlopen.call_args.args[0]
            self.assertIn("/slots/0?action=restore", request.full_url)
            self.assertEqual(json.loads(request.data), {"filename": config.LLAMA_CPP_CACHE_FILE})


if __name__ == "__main__":
    unittest.main()
