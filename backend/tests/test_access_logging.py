import logging

import pytest

from app.core.access_logging import OidcCallbackQueryRedactionFilter


def test_oidc_callback_access_log_redacts_protocol_credentials() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:1000",
            "GET",
            "/api/v1/auth/oidc/callback?code=secret-code&state=secret-state",
            "1.1",
            303,
        ),
        exc_info=None,
    )

    assert OidcCallbackQueryRedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert "/api/v1/auth/oidc/callback HTTP/1.1" in rendered
    assert "secret-code" not in rendered
    assert "secret-state" not in rendered


def test_access_log_redaction_leaves_other_request_targets_unchanged() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1000", "GET", "/api/v1/incidents?page=2", "1.1", 200),
        exc_info=None,
    )

    assert OidcCallbackQueryRedactionFilter().filter(record) is True
    assert "?page=2" in record.getMessage()


@pytest.mark.parametrize(
    "request_target",
    [
        "/api/v1/auth/oidc/callback/?code=secret-code&state=secret-state",
        "/api/v1/auth/oidc/callback%2F?code=secret-code&state=secret-state",
        "/api/v1/auth/oidc/callback%2f?code=secret-code&state=secret-state",
    ],
)
def test_oidc_callback_redirect_variants_also_redact_query(
    request_target: str,
) -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1000", "GET", request_target, "1.1", 307),
        exc_info=None,
    )

    assert OidcCallbackQueryRedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert "secret-code" not in rendered
    assert "secret-state" not in rendered
