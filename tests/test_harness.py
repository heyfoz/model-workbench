import json
import os
import unittest
from unittest.mock import Mock, patch

from server import harness


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.client = harness.app.test_client()
        self.env = patch.dict(os.environ, {
            "CHAT_API_BASE": "https://example.test/v1",
            "CHAT_API_MODEL": "example-model",
            "CHAT_API_KEY": "test-only",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]

    def run_model(self, provider, **options):
        return self.client.post("/api/run", json={
            "model": "example-model", "provider": provider, "messages": self.messages, **options,
        })

    @patch.object(harness, "provider_json")
    def test_native_model_turn_and_usage(self, upstream):
        upstream.side_effect = [
            {"capabilities": ["completion"]},
            {"message": {"content": "four"}, "prompt_eval_count": 8, "eval_count": 2},
        ]
        result = self.run_model("ollama")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["input_tokens"], 8)
        self.assertEqual(result.json["output_tokens"], 2)
        path, payload, key = upstream.call_args.args
        self.assertIsNone(key)
        self.assertIs(payload["stream"], False)
        self.assertEqual(path, harness.OLLAMA + "/api/chat")
        self.assertEqual(payload["messages"], self.messages)

    @patch.object(harness, "provider_json")
    def test_compatible_model_turn_and_optional_usage(self, upstream):
        upstream.return_value = {"choices": [{"message": {"content": "four"}}]}
        result = self.run_model("compatible")
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json["input_tokens"])
        self.assertEqual(result.json["content"], "four")
        path, payload, key = upstream.call_args.args
        self.assertEqual(path, "https://example.test/v1/chat/completions")
        self.assertEqual(payload["messages"], self.messages)
        self.assertEqual(key, "test-only")

    @patch.object(harness, "provider_json")
    def test_picker_excludes_remote_and_decision_models(self, upstream):
        upstream.side_effect = [
            {"models": [{"name": "cloud", "remote_model": "elsewhere"},
                        {"name": "decision"}, {"name": "chat"}]},
            {"capabilities": ["decision"]}, {"capabilities": ["completion"]},
        ]
        result = self.client.get("/api/providers")
        self.assertEqual(result.json[0]["models"], ["chat"])
        self.assertEqual(result.json[1]["location"], "Hosted · example.test")
        self.assertNotIn(b"test-only", result.data)

    @patch.object(harness, "provider_json")
    def test_api_remains_available_when_ollama_is_down(self, upstream):
        upstream.side_effect = harness.ProviderError("Connection failed")
        result = self.client.get("/api/providers")
        self.assertEqual(result.json[0]["models"], [])
        self.assertEqual(result.json[1]["models"], ["example-model"])

    @patch.object(harness, "provider_json")
    def test_bad_history_does_not_call_provider(self, upstream):
        self.messages.append({"role": "assistant", "content": "unfinished"})
        self.assertEqual(self.run_model("compatible").status_code, 400)
        upstream.assert_not_called()

    @patch.object(harness, "provider_json")
    def test_missing_answer_is_reported(self, upstream):
        upstream.return_value = {"choices": []}
        result = self.run_model("compatible")
        self.assertEqual(result.status_code, 502)
        self.assertIn("no assistant text", result.json["error"])

    @patch.object(harness.requests, "request")
    def test_only_configured_api_receives_bearer_header(self, http):
        http.return_value.status_code = 200
        http.return_value.json.side_effect = [
            {"capabilities": ["completion"]}, {"message": {"content": "local"}},
            {"choices": [{"message": {"content": "hosted"}}]},
        ]
        self.assertEqual(self.run_model("ollama").status_code, 200)
        self.assertEqual(self.run_model("compatible").status_code, 200)
        first, second, third = http.call_args_list
        self.assertEqual(first.kwargs["headers"], {})
        self.assertEqual(second.kwargs["headers"], {})
        self.assertEqual(third.kwargs["headers"], {"Authorization": "Bearer test-only"})
        self.assertFalse(third.kwargs["allow_redirects"])

    @patch.object(harness, "provider_response")
    @patch.object(harness, "local_chat_model", return_value=True)
    def test_native_stream_delivers_in_order_and_closes(self, _installed, request):
        consumed = []
        def lines(**kwargs):
            yield b'{"message":{"content":"one "},"done":false}'
            consumed.append(True)
            yield b'{"message":{"content":"two"},"done":true,"prompt_eval_count":10,"eval_count":2}'
        request.return_value.iter_lines.side_effect = lines
        result = self.run_model("ollama", stream=True)
        chunks = iter(result.response)
        self.assertEqual(json.loads(next(chunks)), {"type": "delta", "content": "one "})
        self.assertEqual(consumed, [])
        self.assertEqual(json.loads(next(chunks))["content"], "two")
        final = json.loads(next(chunks))
        self.assertEqual((final["type"], final["input_tokens"], final["output_tokens"]), ("done", 10, 2))
        self.assertTrue(request.call_args.args[1]["stream"])
        result.close()
        request.return_value.close.assert_called_once()

    @patch.object(harness, "provider_response")
    def test_compatible_stream_reads_sse_and_trailing_usage(self, request):
        request.return_value.iter_lines.return_value = [
            b": keepalive", b"", b'data: {"choices":[{"delta":{"role":"assistant"}}]}', b"",
            'data: {"choices":[{"delta":{"content":"Olá"}}]}'.encode(), b"",
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', b"",
            b'data: {"choices":[],', b'data: "usage":{"prompt_tokens":9,"completion_tokens":1}}', b"",
            b'data: [DONE]', b"",
        ]
        result = self.run_model("compatible", stream=True)
        self.assertEqual(result.mimetype, "application/x-ndjson")
        delta, done = map(json.loads, result.data.splitlines())
        self.assertEqual(delta, {"type": "delta", "content": "Olá"})
        self.assertEqual((done["type"], done["input_tokens"], done["output_tokens"]), ("done", 9, 1))
        url, payload, key = request.call_args.args
        self.assertEqual(url, "https://example.test/v1/chat/completions")
        self.assertEqual(key, "test-only")
        self.assertTrue(payload["stream_options"]["include_usage"])
        result.close()

    @patch.object(harness, "provider_response")
    def test_invalid_or_unfinished_stream_never_reports_success(self, request):
        for tail in ([], [b"data: garbage", b""], [b'data: {"error":{}}', b""]):
            with self.subTest(tail=tail):
                request.return_value.iter_lines.return_value = [
                    b'data: {"choices":[{"delta":{"content":"partial"}}]}', b"", *tail,
                ]
                result = self.run_model("compatible", stream=True)
                events = list(map(json.loads, result.data.splitlines()))
                self.assertEqual([e["type"] for e in events], ["delta", "error"])
                result.close()

    @patch.object(harness, "provider_response")
    def test_empty_and_disconnected_streams(self, request):
        upstream = Mock()
        request.return_value = upstream
        upstream.iter_lines.return_value = [b"data: [DONE]", b""]
        result = self.run_model("compatible", stream=True)
        self.assertEqual(json.loads(result.data)["type"], "error")
        result.close()
        upstream.reset_mock()
        upstream.iter_lines.return_value = [b'data: {"choices":[{"delta":{"content":"a"}}]}', b""]
        result = self.run_model("compatible", stream=True)
        next(iter(result.response))
        result.close()
        upstream.close.assert_called_once()
