import pytest
import requests

from stratum_open.model import Model, _billions


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.ok = data, status, status < 400

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, routes):
        self.routes = routes  # (method, url) -> data, or an exception to raise

    def _answer(self, method, url):
        found = self.routes.get((method, url))
        if isinstance(found, Exception):
            raise found
        return FakeResponse(found if found is not None else {}, 200 if found is not None else 404)

    def get(self, url, **kwargs):
        return self._answer("GET", url)

    def post(self, url, **kwargs):
        return self._answer("POST", url)


def test_ollama_reports_size_and_context():
    session = FakeSession({
        ("GET", "http://localhost:11434/api/version"): {"version": "0.40.13"},
        ("POST", "http://localhost:11434/api/show"): {
            "details": {"parameter_size": "7.6B"}, "model_info": {"qwen2.context_length": 32768, "general.architecture": "qwen2"},
        },
    })
    info = Model("http://localhost:11434/v1", "qwen2.5-coder:7b", session=session).describe()
    assert (info.billions, info.context, info.local) == (7.6, 32768, True)



@pytest.mark.parametrize("name, size, profile", [("gpt-oss:20b", "20.9B", "small"), ("codestral:22b", "22.2B", "large")])
def test_gpt_oss_20b_gets_the_small_profile_from_the_size_ollama_reports(name, size, profile):
    """Ollama reports gpt-oss:20b as 20.9B. The README's setup for it was
    measured on the small profile, so `auto` picks small; a 22B model stays large."""
    from stratum_open.engine import effective_policy
    from stratum_open.policy import parse_policy

    session = FakeSession({
        ("GET", "http://localhost:11434/api/version"): {"version": "0.40.13"},
        ("POST", "http://localhost:11434/api/show"): {"details": {"parameter_size": size}},
    })
    model = Model("http://localhost:11434/v1", name, session=session)
    policy, chosen, _ = effective_policy(parse_policy("review:\n  reasoning: low\n  verify: strict\n"), model)
    assert chosen == profile
    if profile == "small":
        assert (policy.review.file_reviews, policy.review.lens_files, policy.review.walkthrough) == (0, 0, False)

def test_a_hosted_server_reports_context_and_the_name_gives_the_size():
    session = FakeSession({
        ("GET", "https://openrouter.ai/api/version"): requests.ConnectionError(),
        ("GET", "https://openrouter.ai/api/v1/models"): {"data": [{"id": "google/gemma-4-31b-it:free", "context_length": 131072}]},
    })
    info = Model("https://openrouter.ai/api/v1", "google/gemma-4-31b-it:free", session=session).describe()
    assert (info.billions, info.context, info.local) == (31, 131072, False)


def test_describe_never_fails():
    session = FakeSession({("GET", "https://example.invalid/api/version"): requests.ConnectionError(),
                           ("GET", "https://example.invalid/v1/models"): requests.ConnectionError()})
    info = Model("https://example.invalid/v1", "mystery", session=session).describe()
    assert (info.billions, info.context, info.local) == (None, None, False)


class Down:
    def get(self, url, **kwargs):
        raise requests.ConnectionError()

    post = get


def test_an_unreachable_model_error_leaves_out_credentials_in_the_endpoint():
    for endpoint, shown in (("https://bob:s3cretpass@gpu.example.org:8443/v1", "https://gpu.example.org:8443/v1"),
                            ("https://gpu.example.org/v1?key=AIzaSyD-abcdef0123456789", "https://gpu.example.org/v1")):
        reply = Model(endpoint, "m", session=Down()).complete("s", "u")
        assert reply.error == f"couldn't reach the model at {shown}: ConnectionError"


@pytest.mark.parametrize("endpoint", ["bob:s3cretpass@gpu.example.org/v1", "localhost:11434/v1", "http://[::1/v1"])
def test_an_endpoint_without_a_scheme_is_named_by_a_fixed_phrase(endpoint):
    reply = Model(endpoint, "m", session=Down()).complete("s", "u")
    assert reply.error == "couldn't reach the model at an endpoint that isn't a valid http:// or https:// URL: ConnectionError"


def test_parameter_sizes():
    assert _billions("7.6B") == 7.6
    assert _billions("8x7B") == 56
    assert _billions("500M") == 0.5
    assert _billions("") is None


@pytest.mark.parametrize("policy, env", [
    ("review:\n  endpoint: off\n", {}),  # YAML reads an unquoted `off` as false
    ("review:\n  endpoint: \"off\"\n", {}),
    ("review:\n  endpoint: \" OFF \"\n", {}),
    ("", {"STRATUM_MODEL_ENDPOINT": "off"}),  # the Action's `model-endpoint: off`, or the App's environment
    # The policy's `off` holds against the environment's server: under the App, whose operator always sets
    # STRATUM_MODEL_ENDPOINT, a repo could otherwise never keep its code from the model.
    ("review:\n  endpoint: off\n", {"STRATUM_MODEL_ENDPOINT": "http://gpu.example:8000/v1"}),
    ("review:\n  endpoint: http://gpu.example:8000/v1\n", {"STRATUM_MODEL_ENDPOINT": "off"}),
])
def test_an_endpoint_of_off_builds_no_model(policy, env):
    from stratum_open.model import model_from_policy
    from stratum_open.policy import parse_policy
    parsed = parse_policy(policy)
    assert parsed.errors == () and model_from_policy(parsed, env=env) is None


def test_any_other_endpoint_still_builds_a_model_with_the_environment_first():
    from stratum_open.model import model_from_policy
    from stratum_open.policy import parse_policy
    policy = parse_policy("review:\n  endpoint: http://gpu.example:8000/v1\n")
    assert model_from_policy(policy, env={}).endpoint == "http://gpu.example:8000/v1"
    assert model_from_policy(policy, env={"STRATUM_MODEL_ENDPOINT": "http://localhost:11434/v1"}).endpoint == "http://localhost:11434/v1"
    assert model_from_policy(parse_policy(""), env={"STRATUM_MODEL_ENDPOINT": ""}).endpoint == "http://localhost:11434/v1"
