"""The model-call seam for the quality judge: a recorded client for CI, a live client for eval time.

The judge is pure everywhere except the one line that asks a model. That line is a
:data:`ModelClient` — a ``prompt -> raw completion text`` callable — so the judge's prompt-building
and response-parsing are exercised offline with :class:`RecordedModelClient` (the recorded-judge
pattern of tech plan §12), while the live call goes through :class:`LiveModelClient` at eval time.

:class:`LiveModelClient` is the human-gated path: it needs credentials and a network, so it is never
run in the offline gate — the same posture as the harness's ``HarborCliExecutor`` and the in-task
verifier's ``LiveJudge``. It imports no model SDK (keeping ``eval/`` light and the gate green); it
speaks the OpenAI-compatible chat-completions shape over ``urllib`` against a configurable endpoint,
which is how Claude on Bedrock is reached in this project's setup. Confirming the exact endpoint and
request shape against a live model is part of the human-gated eval run, mirroring eval/README.md's
open item.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # A chat-completions payload is arbitrary JSON until we read the one field we need.

#: A model call: a rendered prompt in, the model's raw completion text out. The seam a test replaces.
ModelClient = Callable[[str], str]

#: Environment variables that configure the live judge. The model id is configurable and never
#: hardcoded (task What To Build §2); the endpoint defaults to the OpenAI-compatible shape.
_ENDPOINT_ENV = "EVAL_JUDGE_ENDPOINT"
_MODEL_ENV = "EVAL_JUDGE_MODEL"
_API_KEY_ENV = "EVAL_JUDGE_API_KEY"
_DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"

#: The only scheme the live client will open. The endpoint is configurable; its scheme is not — see
#: :meth:`LiveModelClient.require_transport_security`.
REQUIRED_ENDPOINT_SCHEME = "https://"


@dataclass(frozen=True, slots=True)
class RecordedModelClient:
    """Replays one pinned raw completion, ignoring the prompt — the offline stand-in for a live model.

    Each judged datapoint gets its own recorded response, so the alignment set pins a real model
    reply per labeled datapoint and the reward-parsing logic is tested without a model in the tree.
    """

    response: str

    def __call__(self, prompt: str) -> str:  # noqa: ARG002 — a recorded client ignores the prompt by design
        return self.response


@dataclass(frozen=True, slots=True)
class LiveModelClient:
    """Asks a live model for a quality verdict. The human-gated eval-time path; never run in CI.

    Speaks the OpenAI-compatible chat-completions shape over ``urllib`` (no SDK import) against a
    configurable endpoint and model. Credentials and the endpoint come from the environment, so an
    offline gate that never sets them never reaches the network."""

    model: str
    endpoint: str = _DEFAULT_ENDPOINT
    temperature: float = 0.0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> LiveModelClient:
        """Build the live client from the environment — the model id is required, the endpoint optional."""
        source = os.environ if environ is None else environ
        model = source.get(_MODEL_ENV, "")
        if not model:
            raise ValueError(f"the live judge needs a model id in ${_MODEL_ENV} (it is never hardcoded)")
        return cls(model=model, endpoint=source.get(_ENDPOINT_ENV, _DEFAULT_ENDPOINT))

    @staticmethod
    def require_transport_security(endpoint: str) -> None:
        """Refuse an endpoint whose scheme is not ``https://`` before it ever reaches ``urllib``.

        ``urllib`` honours every scheme it knows, ``file://`` included, so an endpoint read from
        ``$EVAL_JUDGE_ENDPOINT`` is a way to turn a model call into a local file read whose contents come
        back as the judge's completion — and, over ``http://``, a way to send the prompt and the bearer
        token in clear text. The endpoint is configurable by design, which is exactly why its scheme is
        not: this refuses anything but TLS rather than trusting whoever set the variable.
        """
        if not endpoint.startswith(REQUIRED_ENDPOINT_SCHEME):
            raise ValueError(
                f"the live judge endpoint must start with {REQUIRED_ENDPOINT_SCHEME!r}, got {endpoint!r} — "
                f"urllib would honour file:// and read a local file as the model's reply"
            )

    def __call__(self, prompt: str) -> str:
        """Send the prompt to the configured model and return its raw completion text (eval-time only)."""
        self.require_transport_security(self.endpoint)
        body = json.dumps(
            {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 — configured https endpoint, not a user-controlled scheme
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {os.environ.get(_API_KEY_ENV, '')}",
                "Content-Type": "application/json",
            },
        )
        # Triaged. The rule's concern is a scheme an attacker chooses, and require_transport_security
        # above has already refused everything but https://, so the file:// read the rule names cannot
        # reach this line. The URL stays dynamic on purpose — the endpoint is configuration — and it is
        # the *scheme* that is fixed. Two tests watch the refusal fire.
        # nosemgrep: dynamic-urllib-use-detected
        with urllib.request.urlopen(request) as response:  # noqa: S310  # nosec B310  # https-only, enforced above
            payload: Any = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        return str(content)
