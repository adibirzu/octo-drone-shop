"""Shop readiness and checkout observability tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace

ROOT = Path(__file__).resolve().parents[1]


def _dict_literal_keys(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    keys = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def test_shop_ready_reports_logging_configuration() -> None:
    source = (ROOT / "server/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    ready_functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "ready"
    ]

    assert ready_functions
    return_dicts = [
        node.value
        for node in ast.walk(ready_functions[0])
        if isinstance(node, ast.Return)
    ]
    ready_keys = set().union(*(_dict_literal_keys(node) for node in return_dicts))

    assert "apm_configured" in ready_keys
    assert "rum_configured" in ready_keys
    assert "logging_configured" in ready_keys


@pytest.fixture
def shop_journey(monkeypatch, request):
    import runpy
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, Mock

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    from server import auth_security, database
    from server.chaos import db_faults
    from server.config import cfg
    from server.middleware import tracing
    from server.modules import (
        integrations,
        java_app_server,
        observability_dashboard,
        payment_gateway_simulation,
        shop,
    )
    from server.observability import (
        correlation,
        log_enricher,
        logging_sdk,
        metrics,
        otel_setup,
    )

    # Use the real routes without starting exporters or installing global hooks.
    for module, name in (
        (otel_setup, "init_otel"),
        (otel_setup, "instrument_fastapi_app"),
        (metrics, "init_metrics"),
        (log_enricher, "install_enricher"),
        (db_faults, "install"),
    ):
        monkeypatch.setattr(module, name, Mock())
    for name in (
        "oracle_dsn",
        "java_apm_service_url",
        "oci_apm_endpoint",
        "oci_apm_rum_endpoint",
        "oci_log_id",
    ):
        monkeypatch.setattr(cfg, name, "")
    monkeypatch.setattr(integrations, "_crm_url", lambda: "")
    service_key = "synthetic-shop-test"
    monkeypatch.setattr(cfg, "internal_service_key", service_key)
    monkeypatch.setattr(cfg, "payment_gateway_simulation_enabled", True)
    monkeypatch.setenv("PAYMENT_SIMULATION_MODE", "approve")

    # Config reload tests replace cfg while these modules keep their imports.
    # Bind the cached routes and their dependencies to the same cfg as /ready.
    for module in (
        auth_security,
        correlation,
        integrations,
        java_app_server,
        logging_sdk,
        observability_dashboard,
        payment_gateway_simulation,
        shop,
        tracing,
    ):
        monkeypatch.setattr(module, "cfg", cfg)

    logs = []
    monkeypatch.setattr(logging_sdk, "_log_queue", Mock(put=logs.append))
    monkeypatch.setattr(logging_sdk, "_security_logger", Mock())
    spans = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    request.addfinalizer(provider.shutdown)
    provider.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)

    engine = create_engine("sqlite://")
    request.addfinalizer(engine.dispose)

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def sqlite_row_limit(conn, cursor, statement, parameters, context, executemany):
        # SQLite uses LIMIT for the row cap shared by the deployed SQL dialects.
        return re.sub(
            r"FETCH FIRST (\d+) ROWS ONLY\s*$", r"LIMIT \1", statement
        ), parameters

    @asynccontextmanager
    async def local_session():
        with Session(engine) as db:
            yield Mock(
                execute=AsyncMock(wraps=db.execute),
                commit=AsyncMock(wraps=db.commit),
                rollback=AsyncMock(wraps=db.rollback),
            )

    monkeypatch.setattr(database, "AsyncSessionLocal", local_session)
    monkeypatch.setattr(observability_dashboard, "AsyncSessionLocal", local_session)
    app = runpy.run_path(str(ROOT / "server/main.py"))["app"]

    database.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            database.Product(id=1, name="Demo product", sku="DEMO-1", price=80, stock=2)
        )
        db.commit()

    return app, service_key, spans, logs


async def test_checkout_payment_evidence_joins_one_ready_shop_transaction(
    shop_journey,
) -> None:
    app, service_key, spans, logs = shop_journey
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://shop.test"
    ) as client:
        readiness = await client.get("/ready")
        assert readiness.status_code == 200
        status = readiness.json()
        assert status["ready"] is True
        assert status["database"] == "connected"
        assert status["apm_configured"] is False
        assert status["rum_configured"] is False
        assert status["logging_configured"] is False

        journey = "synthetic-checkout"
        checkout = await client.post(
            "/api/shop/checkout",
            headers={"X-Octo-Journey-Id": journey},
            json={
                "items": [{"product_id": 1, "quantity": 1}],
                "session_id": journey,
                "customer_email": "buyer@example.invalid",
                "payment_method": "google_pay",
                "payment_details": {
                    "wallet": {"token": "synthetic-wallet-value", "network": "visa"}
                },
            },
        )
        assert checkout.status_code == 200
        receipt = checkout.json()
        assert receipt["status"] == "order_placed"
        assert receipt["payment_status"] == "paid"
        order_id = receipt["order_id"]
        gateway_id = receipt["payment"]["gateway"]["request_id"]
        trace_id = receipt["trace_id"]
        assert order_id > 0 and gateway_id
        assert re.fullmatch(r"[0-9a-f]{32}", trace_id), (
            "checkout receipt needs a joinable trace ID"
        )

        filters = {
            "order_id": order_id,
            "gateway_request_id": gateway_id,
            "trace_id": trace_id,
        }
        path = "/api/observability/payment-gateway/events"
        headers = {"X-Internal-Service-Key": service_key}
        drilldown = await client.get(path, params=filters, headers=headers)
        assert drilldown.status_code == 200
        evidence = drilldown.json()
        assert evidence["summary"]["order_ids"] == [order_id]
        assert evidence["summary"]["gateway_request_ids"] == [gateway_id]
        assert evidence["summary"]["trace_ids"] == [trace_id]
        [final_event] = [
            item
            for item in evidence["events"]
            if item["step"]["name"] == "merchant_authorization_result"
        ]
        assert final_event["step"]["status"] == "completed"
        assert final_event["order"]["payment_status"] == "paid"

        unrelated_trace_id = f"{int(trace_id, 16) ^ 1:032x}"
        wrong_trace = await client.get(
            path,
            params={**filters, "trace_id": unrelated_trace_id},
            headers=headers,
        )
        assert wrong_trace.status_code == 200
        assert wrong_trace.json()["events"] == []

    emitted_spans = spans.get_finished_spans()
    [checkout_span] = [span for span in emitted_spans if span.name == "shop.checkout"]
    [payment_span] = [
        span
        for span in emitted_spans
        if span.attributes.get("payment.gateway.step")
        == "merchant_authorization_result"
    ]
    [checkout_log] = [
        fields for _, message, fields in logs if message == "Store checkout persisted"
    ]
    [payment_log] = [
        fields
        for _, _, fields in logs
        if fields.get("payment.gateway.step") == "merchant_authorization_result"
    ]
    for span, fields in ((checkout_span, checkout_log), (payment_span, payment_log)):
        assert trace.format_trace_id(span.context.trace_id) == trace_id
        assert span.attributes["orders.order_id"] == order_id
        assert span.attributes["payment.gateway.request_id"] == gateway_id
        assert span.attributes["shop.journey_id"] == journey
        assert fields["trace_id"] == fields["oracleApmTraceId"] == trace_id
        assert fields["span_id"] == trace.format_span_id(span.context.span_id)
        assert fields["orders.order_id"] == order_id
        assert fields["payment.gateway.request_id"] == gateway_id
        assert fields["workflow_id"] == "checkout"
        assert fields["shop.journey_id"] == journey
    assert payment_log["span_id"] == final_event["trace"]["span_id"]


async def test_declined_checkout_retains_correlated_failure_evidence(
    shop_journey, monkeypatch
) -> None:
    monkeypatch.setenv("PAYMENT_SIMULATION_MODE", "decline")
    app, service_key, spans, logs = shop_journey
    journey = "synthetic-declined-checkout"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://shop.test") as client:
        response = await client.post(
            "/api/shop/checkout",
            headers={"X-Octo-Journey-Id": journey},
            json={"items": [{"product_id": 1, "quantity": 1}],
                  "session_id": journey, "customer_email": "buyer@example.test",
                  "payment_method": "google_pay",
                  "payment_details": {"wallet": {"token": "synthetic", "network": "visa"}}},
        )
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["payment_status"] == "failed"
        assert receipt["payment"]["status"] == "declined"
        assert receipt["order_status"] != "paid"
        order_id, trace_id = receipt["order_id"], receipt["trace_id"]
        gateway_id = receipt["payment"]["gateway"]["request_id"]
        assert order_id > 0 and gateway_id and re.fullmatch(r"[0-9a-f]{32}", trace_id)
        evidence_response = await client.get(
            "/api/observability/payment-gateway/events",
            params={"order_id": order_id, "gateway_request_id": gateway_id, "trace_id": trace_id},
            headers={"X-Internal-Service-Key": service_key},
        )
        assert evidence_response.status_code == 200
        evidence = evidence_response.json()
        assert evidence["summary"]["order_ids"] == [order_id]
        assert evidence["summary"]["gateway_request_ids"] == [gateway_id]
        assert evidence["summary"]["trace_ids"] == [trace_id]
        [final_event] = [event for event in evidence["events"] if event["step"]["name"] == "merchant_authorization_result"]
        assert final_event["step"]["status"] != "completed"
        assert final_event["order"]["payment_status"] == "failed"

    [checkout_span] = [span for span in spans.get_finished_spans() if span.name == "shop.checkout"]
    [payment_span] = [span for span in spans.get_finished_spans() if span.attributes.get("payment.gateway.step") == "merchant_authorization_result"]
    [checkout_log] = [fields for _, message, fields in logs if message == "Store checkout persisted"]
    [payment_log] = [fields for _, _, fields in logs if fields.get("payment.gateway.step") == "merchant_authorization_result"]
    for span, fields in ((checkout_span, checkout_log), (payment_span, payment_log)):
        assert trace.format_trace_id(span.context.trace_id) == trace_id
        assert span.attributes["orders.order_id"] == fields["orders.order_id"] == order_id
        assert span.attributes["payment.gateway.request_id"] == fields["payment.gateway.request_id"] == gateway_id
        assert span.attributes["shop.journey_id"] == fields["shop.journey_id"] == journey
        assert fields["trace_id"] == fields["oracleApmTraceId"] == trace_id
        assert fields["span_id"] == trace.format_span_id(span.context.span_id)
    assert checkout_log["orders.payment_status"] == "failed"
    assert payment_log["span_id"] == final_event["trace"]["span_id"]
