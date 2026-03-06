"""Structured logging with OCI Logging SDK + Splunk HEC integration.

Supports trace-log correlation for OCI Log Analytics via oracleApmTraceId.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone

from opentelemetry import trace

from server.config import cfg

logger = logging.getLogger(__name__)

_security_logger = logging.getLogger("security.events")
_security_logger.setLevel(logging.INFO)
_security_logger.propagate = False


class _JSONFormatter(logging.Formatter):
    """JSON formatter that injects OTel trace context for Log Analytics correlation."""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": cfg.app_name,
            "runtime": cfg.app_runtime,
        }
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)
        # Inject trace context for OCI Log Analytics correlation
        span = trace.get_current_span()
        if span and span.is_recording():
            ctx = span.get_span_context()
            trace_id_hex = format(ctx.trace_id, "032x")
            log_entry["trace_id"] = trace_id_hex
            log_entry["span_id"] = format(ctx.span_id, "016x")
            # OCI Log Analytics uses this field for APM ↔ Log correlation
            log_entry["oracleApmTraceId"] = trace_id_hex
        return json.dumps(log_entry, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JSONFormatter())
_security_logger.addHandler(_handler)

# OCI Logging SDK client (lazy init)
_oci_logging_client = None


def _get_oci_logging_client():
    global _oci_logging_client
    if _oci_logging_client is not None:
        return _oci_logging_client
    if not cfg.logging_configured:
        return None
    try:
        import oci
        auth_mode = cfg.oci_auth_mode if hasattr(cfg, "oci_auth_mode") else "auto"
        if auth_mode == "instance_principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            _oci_logging_client = oci.loggingingestion.LoggingClient(config={}, signer=signer)
        else:
            try:
                signer = oci.auth.signers.get_resource_principals_signer()
                _oci_logging_client = oci.loggingingestion.LoggingClient(config={}, signer=signer)
            except Exception:
                config = oci.config.from_file()
                _oci_logging_client = oci.loggingingestion.LoggingClient(config)
        return _oci_logging_client
    except Exception:
        return None


def push_log(level: str, message: str, **kwargs):
    """Push a structured log to OCI Logging and optionally Splunk.

    Injects trace_id and oracleApmTraceId for APM ↔ Log Analytics correlation.
    """
    # Inject trace context
    span = trace.get_current_span()
    if span and span.is_recording():
        ctx = span.get_span_context()
        trace_id_hex = format(ctx.trace_id, "032x")
        kwargs["trace_id"] = trace_id_hex
        kwargs["span_id"] = format(ctx.span_id, "016x")
        kwargs["oracleApmTraceId"] = trace_id_hex

    service_name = f"{cfg.app_name}-{cfg.app_runtime}"
    kwargs["app.service"] = service_name
    kwargs["app.runtime"] = cfg.app_runtime

    # Write to structured logger (stdout)
    record = logging.LogRecord(
        name="security.events", level=getattr(logging, level.upper(), logging.INFO),
        pathname="", lineno=0, msg=message, args=(), exc_info=None,
    )
    record.extra_fields = kwargs
    _security_logger.handle(record)

    # Push to OCI Logging SDK
    _push_to_oci_logging(level, message, kwargs)

    # Push to Splunk HEC
    _push_to_splunk(level, message, kwargs)


def _push_to_oci_logging(level: str, message: str, extra: dict):
    client = _get_oci_logging_client()
    if client is None:
        return
    try:
        import oci
        from oci.loggingingestion.models import PutLogsDetails, LogEntryBatch, LogEntry
        entry = LogEntry(
            data=json.dumps({"message": message, **extra}, default=str),
            id=f"octo-{int(time.time() * 1000)}",
            time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        batch = LogEntryBatch(
            defaultloglevel=level.upper(),
            source=f"{cfg.app_name}-{cfg.app_runtime}",
            type="octo-crm-apm",
            entries=[entry],
        )
        client.put_logs(
            log_id=cfg.oci_log_id,
            put_logs_details=PutLogsDetails(
                specversion="1.0",
                log_entry_batches=[batch],
            ),
        )
    except Exception:
        pass  # never break the request


def _push_to_splunk(level: str, message: str, extra: dict):
    if not cfg.splunk_hec_url or not cfg.splunk_hec_token:
        return
    try:
        import httpx
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "message": message,
            **extra,
        }
        httpx.post(
            f"{cfg.splunk_hec_url}/services/collector/event",
            json={"event": event, "sourcetype": "oci:octo-crm-apm:security"},
            headers={"Authorization": f"Splunk {cfg.splunk_hec_token}"},
            verify=False,
            timeout=2.0,
        )
    except Exception:
        pass  # fire-and-forget


def log_security_event(
    vuln_type: str,
    severity: str,
    message: str,
    source_ip: str = "",
    username: str = "",
    payload: str = "",
    **extra,
):
    """Log a security event with standard attributes for Log Analytics correlation."""
    push_log(
        "WARNING" if severity in ("low", "medium") else "ERROR",
        message,
        **{
            "security.attack.detected": True,
            "security.attack.type": vuln_type,
            "security.attack.severity": severity,
            "security.source_ip": source_ip,
            "security.username": username,
            "security.attack.payload": payload[:512] if payload else "",
            **extra,
        },
    )
