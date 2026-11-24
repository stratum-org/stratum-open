"""The model: any OpenAI-compatible chat server, local by default.

When the endpoint is Ollama, its own `/api/chat` is used instead of the
OpenAI-compatible one, because only the native API takes `num_ctx`. Without
it Ollama runs with a small default window and silently drops the start of a
long prompt, which is where the instructions are.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
import yaml

from stratum_open.policy import Policy
from stratum_open.util import redact, retry_after

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
# A server refusing the request itself (too big for its context, say): sending the same input again gets the same.
REFUSED_STATUSES = {400, 413}
_FENCE_ANYWHERE = re.compile(r"```[a-zA-Z]*[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


@dataclass
class Reply:
    text: str
    model: str
    seconds: float
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""
    # The call failed in a way likely to repeat for the same input: the server took the request
    # but didn't answer within the time limit, or refused it (REFUSED_STATUSES). Not a server
    # that couldn't be reached, or one that kept failing (an outage, which passes).
    repeats: bool = False

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "seconds": round(self.seconds, 1), "ok": self.ok, "error": self.error,
            "prompt_tokens": self.prompt_tokens, "output_tokens": self.output_tokens, "repeats": self.repeats,
        }


class Model:
    def __init__(
        self,
        endpoint: str,
        name: str,
        *,
        api_key: str | None = None,
        context_tokens: int = 16384,
        timeout: float = 900,
        retries: int = 2,
        session: requests.Session | None = None,
        reasoning: str = "default",
    ):
        self.endpoint = endpoint.rstrip("/")
        self.name = name
        self.api_key = api_key
        self.context_tokens = context_tokens
        self.timeout = timeout
        self.retries = retries
        self.reasoning = reasoning  # default | low | off, for models that think before they answer
        self.session = session or requests.Session()
        self._ollama: str | None | bool = False  # False = not checked yet
        self._info = None  # what describe() learned; a model doesn't change during a run

    def complete(self, system: str, user: str, *, max_tokens: int = 2048, json_mode: bool = True) -> Reply:
        started = time.monotonic()
        try:
            base = self._ollama_base()
            if base:
                reply = self._ollama_chat(base, system, user, max_tokens, json_mode)
            else:
                reply = self._openai_chat(system, user, max_tokens, json_mode)
        except requests.RequestException as exc:
            reply = Reply("", self.name, 0, error=f"couldn't reach the model at {_shown(self.endpoint)}: {type(exc).__name__}",
                          repeats=isinstance(exc, requests.ReadTimeout))  # it took the request: not an outage
        except ModelError as exc:
            # A server's error text can quote the request back, API key included; it ends up in a PR comment.
            reply = Reply("", self.name, 0, error=redact(str(exc)), repeats=exc.status in REFUSED_STATUSES)
        reply.seconds = time.monotonic() - started
        return reply

    def describe(self):
        """The model's size and context window, as far as its server says,
        for `review.profile: auto`. Never fails: what it can't learn is None."""
        from stratum_open.profiles import ModelInfo, size_from_name

        if self._info is not None:
            return self._info
        billions = context = None
        base = self._ollama_base()
        local = base is not None or (urlparse(self.endpoint).hostname or "") in LOCAL_HOSTS
        try:
            if base:
                data = self.session.post(f"{base}/api/show", json={"model": self.name}, timeout=10).json()
                billions = _billions((data.get("details") or {}).get("parameter_size"))
                context = next((int(v) for k, v in (data.get("model_info") or {}).items()
                                if k.endswith(".context_length") and isinstance(v, (int, float))), None)
            else:
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                data = self.session.get(f"{self.endpoint}/models", headers=headers, timeout=10).json()
                entry = next((m for m in data.get("data", []) if isinstance(m, dict) and m.get("id") == self.name), {})
                found = entry.get("context_length") or entry.get("context_window") or (entry.get("top_provider") or {}).get("context_length")
                context = int(found) if isinstance(found, (int, float)) else None
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            pass
        self._info = ModelInfo(billions if billions is not None else size_from_name(self.name), context, local)
        return self._info

    def _ollama_base(self) -> str | None:
        if self._ollama is False:
            base = re.sub(r"/v1$", "", self.endpoint)
            try:
                response = self.session.get(f"{base}/api/version", timeout=3)
                data = response.json() if response.ok else None
                found = isinstance(data, dict) and "version" in data
            except (requests.RequestException, ValueError):
                found = False
            self._ollama = base if found else None
        return self._ollama or None

    def _ollama_chat(self, base: str, system: str, user: str, max_tokens: int, json_mode: bool) -> Reply:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "options": {"num_ctx": self.context_tokens, "temperature": 0.1, "num_predict": max_tokens},
        }
        if json_mode:
            body["format"] = "json"
        # A thinking model (gpt-oss, Qwen3) spends its output budget on thinking first; on a CPU
        # that can leave no room for the answer. Ollama's `think` takes a level or false.
        if self.reasoning in ("low", "off"):
            body["think"] = "low" if self.reasoning == "low" else False
        try:
            data = self._post(f"{base}/api/chat", body)
        except ModelError as exc:
            if not ("think" in body and exc.status == 400):
                raise
            del body["think"]  # a model or an Ollama that doesn't take it
            data = self._post(f"{base}/api/chat", body)
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else message if isinstance(message, str) else ""
        return Reply(
            content if isinstance(content, str) else "", str(data.get("model") or self.name), 0,
            _count(data.get("prompt_eval_count")), _count(data.get("eval_count")),
        )

    def _openai_chat(self, system: str, user: str, max_tokens: int, json_mode: bool) -> Reply:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning == "low":
            body["reasoning_effort"] = "low"
        # A server that refuses an optional field gets the call again without it, one at a time.
        while True:
            try:
                data = self._post(f"{self.endpoint}/chat/completions", body)
                break
            except ModelError as exc:
                optional = next((key for key in ("reasoning_effort", "response_format") if key in body), None)
                if exc.status != 400 or optional is None:
                    raise
                del body[optional]
        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return Reply(
            content if isinstance(content, str) else "", str(data.get("model") or self.name), 0,
            _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens")),
        )

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """One call with bounded retries. Everything a server can send that
        isn't a JSON object (an HTML error page with a 200, an empty body,
        `null`, a list) is a ModelError, never a traceback."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(self.retries + 1):
            response = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
            if response.status_code in RETRY_STATUSES and attempt < self.retries:
                time.sleep(min(30.0, retry_after(response.headers.get("Retry-After"), 2 * 3**attempt)))
                continue
            if not response.ok:
                raise ModelError(f"the model server answered {response.status_code}: {_excerpt(response.text)}", response.status_code)
            if not response.content.strip():
                raise ModelError("the model server sent an empty reply", response.status_code)
            try:
                data = response.json()
            except ValueError:
                raise ModelError(f"the model server sent something that isn't JSON: {_excerpt(response.text)}", response.status_code) from None
            if not isinstance(data, dict):
                raise ModelError(f"the model server sent JSON that isn't an object: {_excerpt(response.text)}", response.status_code)
            if isinstance(data.get("error"), (str, dict)) and not data.get("choices") and not data.get("message"):
                error = data["error"]
                raise ModelError(f"the model server reported an error: {_excerpt(error if isinstance(error, str) else error.get('message') or json.dumps(error))}", response.status_code)
            return data
        raise ModelError("the model server kept failing", 0)  # pragma: no cover


class ModelError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def is_model_failure(exc: BaseException, model: Any = None) -> bool:
    """Whether an exception raised by a model object means the model or the
    network failed, as opposed to a bug in Stratum: a `ModelError`, a
    `requests` error, any `OSError` (sockets, urllib, aiohttp), or a type the
    model lists in `failure_types` (a client built on another library, httpx
    say, names its own there)."""
    declared = tuple(t for t in getattr(model, "failure_types", ()) or () if isinstance(t, type))
    return isinstance(exc, (ModelError, requests.RequestException, OSError) + declared)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _excerpt(text: str, limit: int = 160) -> str:
    return " ".join((text or "").split())[:limit]


def _shown(endpoint: str) -> str:
    """The endpoint without a user:password or query string, which can hold a key."""
    try:
        url = urlparse(endpoint)
    except ValueError:  # an unclosed [IPv6 address
        url = None
    # Without a scheme, urlparse reads `user:pass@host` as scheme `user` and a path holding the password.
    if url is None or url.scheme not in ("http", "https") or not url.netloc:
        return "an endpoint that isn't a valid http:// or https:// URL"
    return f"{url.scheme}://{url.netloc.rpartition('@')[2]}{url.path}"


def model_off(policy: Policy, env: Mapping[str, str] = os.environ) -> bool:
    """Whether the policy or the environment sets the endpoint to `off`, for
    the checks alone. Either one is enough: the environment's server wins
    over the policy's otherwise, but under the App, whose operator always
    sets one, a repo's `off` would then never keep its code from the model."""
    return any((endpoint or "").strip().lower() == "off"
               for endpoint in (policy.review.endpoint, env.get("STRATUM_MODEL_ENDPOINT")))


def model_from_policy(policy: Policy, env: Mapping[str, str] = os.environ) -> Model | None:
    """The policy names the model; the environment can point at a different
    server (the Action's own Ollama, or a GPU box) and carries the API key,
    which never goes in the policy file. A model whose family needs its own
    reasoning level (gpt-oss) gets it here too, for the commands that call it
    without a review. None when the endpoint is `off`: every caller then runs
    the checks only, as `review --no-model` does."""
    from stratum_open.profiles import for_model

    if model_off(policy, env):
        return None
    endpoint = env.get("STRATUM_MODEL_ENDPOINT") or policy.review.endpoint
    name = env.get("STRATUM_MODEL_NAME") or policy.model_name
    api_key = env.get("STRATUM_MODEL_API_KEY") or None
    return Model(endpoint, name, api_key=api_key, context_tokens=policy.review.context_tokens,
                 reasoning=for_model(policy, name)[0].review.reasoning)


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
_PARAMS = re.compile(r"^\s*(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*([bm])\s*$", re.IGNORECASE)


def _billions(text: Any) -> float | None:
    """Ollama's "7.6B", "8x7B" or "500M", in billions."""
    match = _PARAMS.match(str(text or ""))
    if not match:
        return None
    count, size, unit = match.groups()
    value = float(size) * (int(count) if count else 1)
    return value / 1000 if unit.lower() == "m" else value


def repair_text(text: str) -> str:
    """Undo double-encoded UTF-8 ("lowâ€‘severity" for "low‑severity"),
    which models' replies pick up in transit, and drop U+FFFD, which marks
    bytes lost for good. ftfy only touches text that shows the signature."""
    if not text:
        return text
    try:
        import ftfy

        text = ftfy.fix_text(text)
    except ImportError:  # pragma: no cover  (a dependency; kept optional for embedders)
        pass
    return text.replace("�", "")


def parse_json(text: str) -> Any | None:
    """The first JSON object in a model's reply. Small models wrap it in
    fences (sometimes with prose around them), add a sentence after it, leave
    a trailing comma, or write near-JSON (single quotes, unquoted keys) that
    YAML still reads. Only an object is ever returned, and an object with
    keys beats a stray `{}` in the prose before the answer."""
    if not isinstance(text, str):
        return None
    text = repair_text(text).strip()
    blocks = [m.group(1) for m in _FENCE_ANYWHERE.finditer(text) if "{" in m.group(1)] + [text]
    decoder = json.JSONDecoder()
    empty: dict | None = None
    for block in blocks:
        start = block.find("{")
        tries = 0
        while start >= 0 and tries < 50:
            # A complete but empty object here is a "{}" in the prose before the answer: look past it.
            # A broken object is the answer itself, cut off or near-JSON: stop here and let YAML, then
            # `salvage`, have it, rather than mistake one of its complete inner objects for the answer.
            tries += 1
            body = block[start:]
            decoded = None
            for candidate in (body, _TRAILING_COMMA.sub(r"\1", body)):
                try:
                    decoded, _ = decoder.raw_decode(candidate)
                    break
                except ValueError:
                    continue
            if decoded is None:
                break
            if isinstance(decoded, dict) and decoded:
                return decoded
            if isinstance(decoded, dict) and empty is None:
                empty = decoded
            start = block.find("{", start + 1)
        end = block.rfind("}")
        if start >= 0 and start < end:
            try:
                value = yaml.safe_load(block[start: end + 1])
            except yaml.YAMLError:
                value = None
            if isinstance(value, dict) and value:
                return value
    return empty


def salvage(text: str) -> dict[str, Any] | None:
    """What can still be used from a reply that isn't one valid object,
    usually because the model ran out of tokens mid-answer: the short text
    fields, and every complete finding before the break. None if there's
    nothing."""
    text = repair_text(text or "")
    out: dict[str, Any] = {}
    for key in ("summary", "matches_description", "mismatch"):
        match = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if match:
            try:
                out[key] = json.loads(f'"{match.group(1)}"')
            except ValueError:
                out[key] = match.group(1)
    findings: list[dict[str, Any]] = []
    start = re.search(r'"findings"\s*:\s*\[', text)
    if start:
        decoder, index = json.JSONDecoder(), start.end()
        while True:
            while index < len(text) and text[index] in " \t\r\n,":
                index += 1
            if index >= len(text) or text[index] != "{":
                break
            try:
                item, index = decoder.raw_decode(text, index)
            except ValueError:
                break
            if isinstance(item, dict):
                findings.append(item)
    if not out and not findings:
        return None
    out["findings"] = findings
    return out
