"""Configuration schema (pydantic models).

Sprint 1 modelled ``server`` + ``storage``. Sprint 3 adds ``validators`` —
a list of per-path validator entries. ``tunnel`` arrives in Sprint 4.

The schema mirrors the YAML structure documented in SPEC §6.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ValidatorType = Literal["github", "stripe", "generic_hmac", "quo", "secret_token"]
TunnelProviderType = Literal["none", "ngrok", "cloudflare"]


class ServerConfig(BaseModel):
    """HTTP server configuration."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="0.0.0.0", description="Bind host.")
    port: int = Field(default=8000, ge=1, le=65535, description="Bind port.")
    log_level: str = Field(default="INFO", description="Log level name.")
    log_format: str = Field(
        default="json",
        description="Log output format: 'json' (default) or 'console' (human-readable).",
    )


class StorageConfig(BaseModel):
    """Storage backend configuration."""

    model_config = ConfigDict(extra="forbid")

    backend: str = Field(default="sqlite", description="Storage backend name.")
    path: str = Field(
        default="data/hooky.db",
        description=(
            "Filesystem path for SQLite-backed storage. Use ':memory:' for an "
            "in-memory database (typical in tests)."
        ),
    )
    retention_days: int = Field(
        default=30,
        ge=0,
        description=(
            "Retention period in days. 0 means keep forever. "
            "(Enforcement not in Sprint 1.)"
        ),
    )


class ValidatorConfig(BaseModel):
    """A single per-path validator config entry.

    A single ``HookyConfig.validators`` list holds zero or more of these.
    Type-specific fields (``header``, ``algorithm``, ``prefix``,
    ``tolerance_seconds``) are validated at validator-build time
    (:mod:`hooky.validators.registry`) rather than via pydantic
    discriminated unions — keeps the YAML shape forgiving and the runtime
    error messages precise.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description=(
            "Full webhook path this validator guards "
            "(e.g. '/webhook/github'). Exact match against incoming request path."
        ),
    )
    type: ValidatorType = Field(
        description=(
            "Validator type: 'github', 'stripe', 'generic_hmac', 'quo', or "
            "'secret_token' (header-equals a shared secret, e.g. Telegram)."
        ),
    )
    secret_env: str = Field(
        description=(
            "Name of the environment variable holding the shared secret. "
            "Sourced via os.environ at app construction time."
        ),
    )

    # generic_hmac + secret_token:
    header: str | None = Field(
        default=None,
        description=(
            "Header name carrying the signature (generic_hmac) or the shared "
            "secret (secret_token; defaults to Telegram's header if omitted)."
        ),
    )
    algorithm: str | None = Field(
        default=None,
        description="'sha256' (default) or 'sha1' (generic_hmac only).",
    )
    prefix: str | None = Field(
        default=None,
        description="Optional signature prefix to strip, e.g. 'sha256=' (generic_hmac only).",
    )

    # stripe + quo:
    tolerance_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Max allowed clock skew between signature timestamp and now. "
            "For 'stripe', defaults to 300s if unset. For 'quo', unset means "
            "no freshness check (HMAC integrity still enforced)."
        ),
    )


class NgrokConfig(BaseModel):
    """ngrok-specific tunnel options. ``auth_token`` sourced from env."""

    model_config = ConfigDict(extra="forbid")

    region: str = Field(default="us", description="Ngrok region (us, eu, ap, au, sa, jp, in).")
    subdomain: str | None = Field(
        default=None,
        description="Optional custom subdomain (requires paid plan).",
    )


class CloudflareConfig(BaseModel):
    """Cloudflare Tunnel options. ``token`` sourced from env CLOUDFLARED_TOKEN."""

    model_config = ConfigDict(extra="forbid")

    # SPEC §6 used 'tunnel_name'; we accept either 'tunnel_name' or 'hostname'
    # for forward-compat. At least one must be set.
    tunnel_name: str | None = Field(
        default=None,
        description=(
            "Legacy field name from the SPEC. The configured tunnel "
            "identifier on Cloudflare's side; used as the public hostname "
            "when 'hostname' is not set."
        ),
    )
    hostname: str | None = Field(
        default=None,
        description="Public hostname for the tunnel (e.g. 'hooky-main.example.com').",
    )


class TunnelConfig(BaseModel):
    """Tunnel subsystem configuration.

    Selects a provider and carries provider-specific sub-config. ``none`` is
    the default — safe for local dev, no external dependencies.
    """

    model_config = ConfigDict(extra="forbid")

    provider: TunnelProviderType = Field(
        default="none",
        description="'none' (localhost), 'ngrok', or 'cloudflare'.",
    )
    ngrok: NgrokConfig = Field(default_factory=NgrokConfig)
    cloudflare: CloudflareConfig = Field(default_factory=CloudflareConfig)


class HandlerConfig(BaseModel):
    """A single handler to load + register at startup.

    Handlers are loaded by dotted import path (the uvicorn/celery idiom).
    Two forms are accepted:

    - ``"package.module:ClassName"`` (canonical, colon-separated)
    - ``"package.module.ClassName"`` (last dotted segment is the attribute)

    Optional ``config`` kwargs are passed to the handler constructor:
    ``HandlerClass(**config)``. Handlers with no required constructor args
    omit it. This is how a consumer registers its handler without writing
    any launcher code — just config.

    Secrets are supplied via ``secret_env`` — a map of ``kwarg_name ->
    ENV_VAR_NAME``. At build time the framework reads each env var and passes
    its value as that constructor kwarg, failing loud if the var is unset.
    This keeps secrets out of both ``config.yaml`` and the handler's own
    code (the handler just takes ``api_token`` as a plain constructor arg),
    and mirrors the ``secret_env`` indirection used by validators. A kwarg
    appearing in both ``config`` and ``secret_env`` is a config error.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description=(
            "Dotted import path to a WebhookHandler subclass, e.g. "
            "'myapp.handlers:SmsHandler'."
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Non-secret keyword args passed to the handler constructor.",
    )
    secret_env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of constructor kwarg name -> environment variable name. Each "
            "env var is read at build time and passed as that kwarg. Keeps "
            "secrets out of config.yaml. Unset env vars fail loud at startup."
        ),
    )


class HookyConfig(BaseModel):
    """Top-level Hooky configuration.

    Sprint 1 shipped ``server`` and ``storage``. Sprint 3 added
    ``validators``. Sprint 4 added ``tunnel``. Sprint 5 adds ``handlers``.
    """

    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    validators: list[ValidatorConfig] = Field(
        default_factory=list,
        description="Per-path signature validators. Empty = no validation.",
    )
    tunnel: TunnelConfig = Field(
        default_factory=TunnelConfig,
        description="Tunnel provider configuration.",
    )
    handlers: list[HandlerConfig] = Field(
        default_factory=list,
        description="Handlers to load by dotted path + register at startup.",
    )
