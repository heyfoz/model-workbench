"""Local model harness. Run with python server/harness.py."""

import argparse
import json
import os
from time import monotonic
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, jsonify, render_template, request


app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=512 * 1024, TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
OLLAMA = "http://127.0.0.1:11434"


class ProviderError(Exception):
    pass


def local_address(host):
    return host in {"localhost", "127.0.0.1", "::1"}


def compatible_profile():
    base = os.environ.get("CHAT_API_BASE", "").strip().rstrip("/")
    model = os.environ.get("CHAT_API_MODEL", "").strip()
    if not base or not model:
        return None
    url = urlsplit(base)
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or not (url.scheme == "https" or (url.scheme == "http" and local_address(url.hostname)))):
        raise ValueError("CHAT_API_BASE requires HTTPS or a loopback HTTP address.")
    return base, model


def provider_response(url, payload=None, key=None, *, stream=False):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = requests.request(
            "GET" if payload is None else "POST", url, json=payload, headers=headers,
            timeout=(4, 120 if url.endswith(("/chat", "/completions")) else 8),
            allow_redirects=False, stream=stream,
        )
    except requests.Timeout as exc:
        raise ProviderError("The model server timed out. Retry or choose another model.") from exc
    except requests.RequestException as exc:
        raise ProviderError("Could not reach the model server. Check that it is running.") from exc
    if not 200 <= response.status_code < 300:
        response.close()
        hint = {401: "Check the server-side API key.", 403: "Check model access.",
                404: "Check the model name and endpoint.", 429: "Try again later."}
        raise ProviderError(f"Provider returned HTTP {response.status_code}. "
                            + hint.get(response.status_code, "Check the model and request size."))
    return response


def provider_json(url, payload=None, key=None):
    response = provider_response(url, payload, key)
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError("The model server returned invalid JSON.") from exc
    finally:
        response.close()
    if not isinstance(data, dict) or data.get("error"):
        raise ProviderError("The model server returned an unsuccessful response.")
    return data


def sse_data(lines):
    """Read UTF-8 data fields from complete Server-Sent Events."""
    fields = []
    for line in lines:
        if not line:
            if fields:
                yield b"\n".join(fields)
                fields.clear()
        elif line.startswith(b"data:"):
            fields.append(line[5:].removeprefix(b" "))


def chat_chunks(lines, provider):
    """Yield provider events and a terminal marker. EOF alone is incomplete."""
    compatible = provider == "compatible"
    for raw in sse_data(lines) if compatible else filter(None, lines):
        if compatible and raw == b"[DONE]":
            yield None
            return
        data = json.loads(raw)
        if not isinstance(data, dict) or "error" in data:
            raise ProviderError("The provider interrupted the stream.")
        yield data
        if not compatible and data.get("done") is True:
            yield None
            return
    raise ProviderError("The connection closed before the response was complete.")


def reply_fields(data, provider, *, streaming=False):
    if provider == "ollama":
        message = data.get("message")
        tokens_in, tokens_out = data.get("prompt_eval_count"), data.get("eval_count")
    else:
        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        key = "delta" if streaming else "message"
        message = first.get(key) if isinstance(first, dict) else None
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else "", tokens_in, tokens_out


def stream_reply(upstream, provider, model, start):
    def encode(**event):
        return json.dumps(event, ensure_ascii=False) + "\n"

    def generate():
        has_text = False
        stats = dict(model=model, provider=provider, input_tokens=None, output_tokens=None)
        try:
            for data in chat_chunks(upstream.iter_lines(chunk_size=256), provider):
                if data is None:
                    if not has_text:
                        raise ProviderError("The provider returned no assistant text.")
                    yield encode(type="done", elapsed_ms=round((monotonic() - start) * 1000), **stats)
                    return
                content, tokens_in, tokens_out = reply_fields(data, provider, streaming=True)
                if data.get("model"):
                    stats["model"] = data["model"]
                if tokens_in is not None:
                    stats["input_tokens"] = tokens_in
                if tokens_out is not None:
                    stats["output_tokens"] = tokens_out
                if content:
                    has_text = has_text or bool(content.strip())
                    yield encode(type="delta", content=content)
        except ProviderError as exc:
            yield encode(type="error", error=str(exc))
        except (ValueError, requests.RequestException):
            yield encode(type="error", error="The model stream was interrupted or malformed.")

    response = Response(generate(), mimetype="application/x-ndjson",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    response.call_on_close(upstream.close)
    return response


def local_chat_model(name):
    info = provider_json(OLLAMA + "/api/show", {"model": name})
    return "completion" in (info.get("capabilities") or []) and not info.get("remote_model")


@app.get("/")
def index():
    return render_template("harness.html")


@app.get("/api/providers")
def providers():
    models = []
    issue = ""
    try:
        for item in provider_json(OLLAMA + "/api/tags").get("models", []):
            name = item["name"]
            if not item.get("remote_model") and local_chat_model(name):
                models.append(name)
        if not models:
            issue = "Install a chat model with ollama pull smollm2:135m, then refresh."
    except (ProviderError, KeyError, TypeError) as exc:
        issue = str(exc) if isinstance(exc, ProviderError) else "Invalid Ollama model list."
    entries = [dict(id="ollama", label="Ollama", location="On this Mac" if os.sys.platform == "darwin"
                    else "On this computer", models=sorted(models), issue=issue)]
    profile = compatible_profile()
    if profile:
        base, model = profile
        host = urlsplit(base)
        location = "Loopback" if local_address(host.hostname) else "Hosted"
        entries.append(dict(id="compatible", label="Chat Completions API", models=[model],
                            location=f"{location} · {host.netloc}", issue=""))
    return jsonify(entries)


def validated_messages(value):
    if not isinstance(value, list) or not value or len(value) % 2 == 0:
        raise ValueError("Send a conversation ending with a user message.")
    if len(value) > 99:
        raise ValueError("Start a new chat after 50 turns.")
    result = []
    for index, item in enumerate(value):
        role = "assistant" if index % 2 else "user"
        if not isinstance(item, dict) or item.get("role") != role:
            raise ValueError("Messages must alternate between user and assistant.")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Messages must contain text.")
        result.append({"role": role, "content": content})
    return result


@app.post("/api/run")
def run():
    body = request.get_json()
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object.")
    provider, model = body.get("provider"), body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Choose a model before sending.")
    messages = validated_messages(body.get("messages"))
    streaming = body.get("stream", False)
    if not isinstance(streaming, bool):
        raise ValueError("The stream option must be true or false.")
    payload = {"model": model, "messages": messages, "stream": streaming}
    start = monotonic()
    key = None
    if provider == "ollama":
        if not local_chat_model(model):
            raise ValueError("Choose an installed local chat model.")
        url = OLLAMA + "/api/chat"
    elif provider == "compatible":
        profile = compatible_profile()
        if not profile or model != profile[1]:
            raise ValueError("Choose the configured API model.")
        url = profile[0] + "/chat/completions"
        key = os.getenv("CHAT_API_KEY")
        if streaming:
            payload["stream_options"] = {"include_usage": True}
    else:
        raise ValueError("Unknown provider.")
    if streaming:
        upstream = provider_response(url, payload, key, stream=True)
        return stream_reply(upstream, provider, model, start)
    data = provider_json(url, payload, key)
    text, input_tokens, output_tokens = reply_fields(data, provider)
    if not text.strip():
        raise ProviderError("The provider returned no assistant text.")
    return jsonify(content=text, model=data.get("model") or model, provider=provider,
                   elapsed_ms=round((monotonic() - start) * 1000),
                   input_tokens=input_tokens, output_tokens=output_tokens)


@app.errorhandler(ValueError)
def invalid_input(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(ProviderError)
def provider_failed(error):
    return jsonify(error=str(error)), 502


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4397)
    args = parser.parse_args()
    compatible_profile()
    app.run(host="127.0.0.1", port=args.port)
