"""
Centralised structured logging for BuchiMaker.

Constitution requirement: "Application must log all API usage
(who did what action on what targets and when). Default log timezone is local."

Every API call is logged with: timestamp (local tz), client_ip, method,
path, status_code, and duration_ms.  The audit logger writes to a
dedicated 'buchimaker.audit' channel so audit records can be shipped to a
separate sink without touching the application log.
"""

import logging
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import SysLogHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.audit_pipeline import enqueue_audit_record


def _get_tz(tz_name: str):
    """Resolve a timezone by name, falling back to local system time.

    Args:
        tz_name: IANA timezone string (e.g. "America/Chicago") or "local".

    Returns:
        A ZoneInfo instance, or None (which lets datetime use local time).
    """
    if tz_name.lower() == "local":
        return None
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        logging.warning("Unknown log_timezone '%s', falling back to local.", tz_name)
        return None


def configure_logging(debug: bool = False, log_timezone: str = "local") -> None:
    """Bootstrap structlog with JSON rendering and correct timezone.

    Args:
        debug: When True sets log level to DEBUG; otherwise INFO.
        log_timezone: IANA timezone name or "local" for the system default.
    """
    tz = _get_tz(log_timezone)

    def _add_timestamp(_, __, event_dict):
        now = datetime.now(tz=tz or timezone.utc)
        event_dict["timestamp"] = now.isoformat()
        return event_dict

    log_level = logging.DEBUG if debug else logging.INFO

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _add_timestamp,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    # Suppress noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str = "buchimaker"):
    """Return a bound structlog logger.

    Args:
        name: Logger name / channel identifier.

    Returns:
        A structlog BoundLogger instance.
    """
    return structlog.get_logger(name)


class TLSSysLogHandler(SysLogHandler):
    """Custom SysLogHandler that wraps the socket in TLS for secure transport."""
    def __init__(self, address, tls_enabled=False, cert_path=None, key_path=None, ca_cert_path=None, **kwargs):
        self.tls_enabled = tls_enabled
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_cert_path = ca_cert_path
        if tls_enabled:
            kwargs['socktype'] = socket.SOCK_STREAM
        super().__init__(address, **kwargs)

    def _connect_sockets(self, address):
        super()._connect_sockets(address)
        if self.socktype == socket.SOCK_STREAM and getattr(self, 'tls_enabled', False):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if self.ca_cert_path:
                context.load_verify_locations(cafile=self.ca_cert_path)
                context.verify_mode = ssl.CERT_REQUIRED
            else:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            if self.cert_path and self.key_path:
                context.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
            self.socket = context.wrap_socket(self.socket, server_hostname=address[0])


_active_syslog_handler = None
_syslog_config_lock = threading.Lock()

def configure_syslog(enabled: bool, host: str, port: int, tls_enabled: bool = False,
                     cert_path: str = None, key_path: str = None,
                     ca_cert_path: str = None):
    """Apply Syslog settings to the dedicated syslog-sink logger.

    Attaches to ``"buchimaker.audit.syslog_sink"`` — a channel only ever
    written to by the background ``AuditWriterThread`` (see
    ``app.core.audit_pipeline``), never by ``AuditLogMiddleware`` directly.
    This keeps syslog's blocking socket I/O (TCP connect, TLS/mTLS
    handshake) off the request-handling thread entirely: a slow or
    unreachable remote syslog server can only ever delay the background
    writer's own flush cadence, never an HTTP response.

    This function itself still performs a blocking connect attempt when
    constructing a TCP+TLS handler (up to the OS TCP timeout if the target
    is unreachable) — callers (``general_settings._apply_syslog()``) run it
    on its own one-off background thread rather than calling it inline, so
    neither a settings-save request nor application startup ever blocks on
    it. ``_syslog_config_lock`` only guards the handler-swap against two
    such background threads racing each other on rapid successive saves.
    """
    global _active_syslog_handler
    with _syslog_config_lock:
        audit_logger = logging.getLogger("buchimaker.audit.syslog_sink")
        audit_logger.propagate = False

        if _active_syslog_handler:
            audit_logger.removeHandler(_active_syslog_handler)
            _active_syslog_handler.close()
            _active_syslog_handler = None

        if not enabled or not host or not port:
            return

        try:
            handler = TLSSysLogHandler(
                address=(host, port),
                tls_enabled=tls_enabled,
                cert_path=cert_path,
                key_path=key_path,
                ca_cert_path=ca_cert_path
            )
            formatter = logging.Formatter('BuchiMakerAudit: %(message)s')
            handler.setFormatter(formatter)
            audit_logger.addHandler(handler)
            _active_syslog_handler = handler
            logging.getLogger("buchimaker").info(f"Syslog configured successfully for {host}:{port}")
        except Exception as exc:
            logging.getLogger("buchimaker").error(f"Failed to configure Syslog: {exc}")


# Endpoints excluded from the persisted/queryable audit trail (DuckDB +
# syslog) — NOT from the stdout structlog line above, which still logs every
# request unconditionally per the Constitution's "log all API usage"
# requirement. These are pure reads of the audit log itself: browsing the
# Audit Logs page (pagination, search) or exporting it otherwise floods the
# very trail being viewed with "someone viewed the audit trail" noise. This
# is safe to exclude because there is no mutation endpoint for audit
# records — nothing here could be used to hide tampering, only to view.
_AUDIT_COLLECTION_EXCLUDED_PATHS = frozenset({
    "/api/v1/system/audit-logs",
    "/api/v1/system/audit-logs/export",
})


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that emits one structured audit record per request.

    Each record contains: timestamp, client_ip, http_method, path,
    query_string, status_code, duration_ms.  Records are written to the
    'buchimaker.audit' logger so they can be routed independently.

    This satisfies the Constitution's requirement that all API usage is logged.
    """

    _audit = structlog.get_logger("buchimaker.audit")

    async def dispatch(self, request: Request, call_next) -> Response:
        """Log request metadata before and after processing.

        Args:
            request: Incoming HTTP request.
            call_next: ASGI callable for the next middleware / route handler.

        Returns:
            The HTTP response produced by the application.
        """
        t_start = time.perf_counter()
        client_ip = request.client.host if request.client else "unknown"

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - t_start) * 1000, 2)

        self._audit.info(
            "api_call",
            client_ip=client_ip,
            method=request.method,
            path=request.url.path,
            query=str(request.url.query),
            status_code=response.status_code,
            duration_ms=duration_ms,
        )

        # Persistence (DuckDB) and syslog export are handled entirely by a
        # background thread — this is a single non-blocking queue put, never
        # I/O, so a slow/unreachable syslog server or a DB hiccup can never
        # add latency here. See app.core.audit_pipeline.
        if request.url.path not in _AUDIT_COLLECTION_EXCLUDED_PATHS:
            # Populated by app.core.auth.require_authentication, which runs
            # as a route dependency during call_next() above and stashes the
            # resolved Principal on request.state — still None for routes
            # with no auth dependency (health, /auth/* itself) or if auth
            # failed before a route was matched (e.g. a 401/404).
            principal = getattr(request.state, "principal", None)
            enqueue_audit_record({
                "ts": datetime.now(),
                "client_ip": client_ip,
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query),
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "user_email": (principal.email or principal.name) if principal and not principal.is_anonymous else None,
            })

        return response
