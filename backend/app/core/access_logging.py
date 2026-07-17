"""Access-log policy for endpoints that receive protocol credentials in queries."""

import logging
from urllib.parse import unquote

_OIDC_CALLBACK_PATH = "/api/v1/auth/oidc/callback"


class OidcCallbackQueryRedactionFilter(logging.Filter):
    """Strip OIDC callback query strings from Uvicorn's structured log arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if not isinstance(arguments, tuple) or len(arguments) < 3:
            return True
        request_target = arguments[2]
        if not isinstance(request_target, str) or "?" not in request_target:
            return True
        raw_path, _, _query = request_target.partition("?")
        if unquote(raw_path).rstrip("/") != _OIDC_CALLBACK_PATH:
            return True
        sanitized = list(arguments)
        sanitized[2] = raw_path
        record.args = tuple(sanitized)
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(item, OidcCallbackQueryRedactionFilter) for item in logger.filters):
        return
    logger.addFilter(OidcCallbackQueryRedactionFilter())
