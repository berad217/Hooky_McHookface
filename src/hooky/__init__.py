"""Hooky_McHookface: async-native, framework-grade webhook receiver.

Public API:

Storage
- :class:`hooky.storage.base.StorageBackend` — storage ABC.
- :class:`hooky.storage.models.WebhookRequest` — in-flight webhook model.
- :class:`hooky.storage.models.StoredWebhook` — stored webhook model.
- :class:`hooky.storage.models.HandlerResult` — handler run result model.

Handlers
- :class:`hooky.handlers.base.WebhookHandler` — handler ABC (the API you subclass).
- :class:`hooky.handlers.manager.HandlerManager` — register + dispatch.
- :class:`hooky.handlers.echo.EchoHandler` — reference handler (returns body).
- :class:`hooky.handlers.logger.LoggerHandler` — reference handler (logs at INFO).

Validators (Sprint 3)
- :class:`hooky.validators.base.SignatureValidator` — validator ABC.
- :class:`hooky.validators.generic.GenericHMACValidator`
- :class:`hooky.validators.github.GitHubValidator`
- :class:`hooky.validators.stripe.StripeValidator`

Tunnels (Sprint 4)
- :class:`hooky.tunnel.base.TunnelProvider` — tunnel ABC.
- :class:`hooky.tunnel.none_provider.NoTunnelProvider`
- :class:`hooky.tunnel.ngrok.NgrokProvider`
- :class:`hooky.tunnel.cloudflare.CloudflareProvider`

Replay (Sprint 5)
- :func:`hooky.replay.engine.replay_webhook` — re-dispatch a stored webhook.

App
- :func:`hooky.app.create_app` — FastAPI app factory.
"""

from hooky.handlers.base import WebhookHandler
from hooky.handlers.echo import EchoHandler
from hooky.handlers.logger import LoggerHandler
from hooky.handlers.manager import HandlerManager
from hooky.storage.base import StorageBackend
from hooky.storage.models import HandlerResult, StoredWebhook, WebhookRequest
from hooky.tunnel.base import TunnelHealth, TunnelProvider, TunnelStatus
from hooky.validators.base import SignatureValidator

__version__ = "0.5.0"

__all__ = [
    "EchoHandler",
    "HandlerManager",
    "HandlerResult",
    "LoggerHandler",
    "SignatureValidator",
    "StorageBackend",
    "StoredWebhook",
    "TunnelHealth",
    "TunnelProvider",
    "TunnelStatus",
    "WebhookHandler",
    "WebhookRequest",
    "__version__",
]
