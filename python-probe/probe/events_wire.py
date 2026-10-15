"""The events-* subcommands: publish and consume real messages through SNS/SQS
(floci) with eventcommon, recording what was sent and what a handler saw — the mirror
of go-probe's events.go, same JSON shapes. Also provisions the topic/queue topology and
sends the hand-crafted bodies of events_raw_bodies.json. See README.md § events-wire-check."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import boto3
import pgcommon
from eventcommon import hmac as event_hmac
from eventcommon.codec import GlueDecodeCodec
from eventcommon.consumer import SQSConfig, SQSConsumer
from eventcommon.context import source_message_from_context
from eventcommon.envelope import Envelope, new_envelope, trace_id_from_context
from eventcommon.outbox import Runner, RunnerConfig, apply_schema, enqueue, enqueue_ordered
from eventcommon.publisher import SNSConfig, SNSPublisher
from opentelemetry import baggage, propagate, trace
from opentelemetry import context as otel_context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from probe.util import print_json

SIGNATURE_ATTRIBUTE = "XSignature"
REGION = "us-east-1"

for _key, _value in (("AWS_ACCESS_KEY_ID", "test"), ("AWS_SECRET_ACCESS_KEY", "test")):
    os.environ.setdefault(_key, _value)


def _tracing() -> InMemorySpanExporter:
    """An SDK tracer provider (recording spans, so consumer span links can be read back),
    the W3C trace-context + baggage propagators, and the producer's baggage."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    otel_context.attach(baggage.set_baggage("probe.lang", "py"))
    return exporter


class GlueEncodeCodec(GlueDecodeCodec):
    """Writes the AWS Glue Schema Registry wire format; decoding is the library's."""

    async def encode(self, event_type: str, payload: bytes) -> tuple[bytes, str]:
        schema = uuid.uuid4()
        return b"\x03\x00" + schema.bytes + payload, str(schema)


def _envelope(case: dict[str, Any]) -> Envelope[Any]:
    o = case.get("opts") or {}
    payload = case["payload"]
    if case.get("payload_blob_bytes"):
        payload = {"blob": "x" * case["payload_blob_bytes"]}
    return new_envelope(
        case["event_type"],
        case["source"],
        payload,
        tenant_id=o.get("tenant_id", ""),
        system_tenant=bool(o.get("system_tenant")),
        trace_id=o.get("trace_id", ""),
        correlation_id=o.get("correlation_id", ""),
        schema_version=o.get("specversion", ""),
        subject=o.get("subject", ""),
        actor=o.get("actor", ""),
        ip_address=o.get("ip_address", ""),
        user_agent=o.get("user_agent", ""),
    )


async def _publish(a: argparse.Namespace) -> dict[str, Any]:
    _tracing()
    with open(a.cases, encoding="utf-8") as f:
        cases = [c for c in json.load(f) if not (a.fifo and c.get("skip_fifo"))]
    envs = [_envelope(c) for c in cases]
    groups = {e.id: c.get("group", "") for e, c in zip(envs, cases, strict=True)}
    sent: list[dict[str, Any]] = [
        {"key": c["key"], "group": c.get("group", ""), "envelope": json.loads(e.to_json())}
        for c, e in zip(cases, envs, strict=True)
    ]

    def publisher(attrs: dict[str, str] | None) -> SNSPublisher:
        return SNSPublisher(
            SNSConfig(topic_arn=a.topic, region=REGION, endpoint_url=a.endpoint),
            message_group_id=(lambda e: groups[e.id]) if a.fifo else None,
            attributes=attrs,
            codec=GlueEncodeCodec() if a.codec == "glue" else None,
        )

    tracer = trace.get_tracer("python-probe")
    if a.mode == "batch":
        with tracer.start_as_current_span("probe.publish_batch") as span:
            async with publisher(None) as pub:
                await pub.publish_batch(envs)
        for s in sent:
            s["producer_trace_id"] = format(span.get_span_context().trace_id, "032x")
    else:
        for s, env in zip(sent, envs, strict=True):
            attrs = None
            if a.hmac:
                s["signature"] = event_hmac.sign_envelope(a.hmac.encode(), env)
                attrs = {SIGNATURE_ATTRIBUTE: s["signature"]}
            with tracer.start_as_current_span("probe.publish") as span:
                async with publisher(attrs) as pub:
                    await pub.publish(env)
            s["producer_trace_id"] = format(span.get_span_context().trace_id, "032x")
    return {"language": "python", "sent": sent}


async def _consume(a: argparse.Namespace) -> dict[str, Any]:
    exporter = _tracing()
    got: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def handler(env: Envelope[Any]) -> None:
        raw = env.wire_data_bytes
        guc = pgcommon.guc_set_from_context()
        src = source_message_from_context()
        r: dict[str, Any] = {
            "envelope": json.loads(env.to_json()),
            "payload_raw": raw.decode() if raw is not None else json.dumps(env.data),
            "trace_id_ctx": trace_id_from_context(),
            "tenant_guc": guc.tenant_id if guc is not None else "",
            "has_tenant_guc": guc is not None,
            "attributes": dict(src.attributes) if src is not None else {},
            "baggage": {k: str(v) for k, v in baggage.get_all().items()},
            "_span_id": trace.get_current_span().get_span_context().span_id,
        }
        if a.hmac:
            r["hmac_ok"] = event_hmac.verify_envelope(
                a.hmac.encode(), env, r["attributes"].get(SIGNATURE_ATTRIBUTE, "")
            )
        got.append(r)
        if len(got) == a.n:
            done.set()

    consumer = SQSConsumer(
        SQSConfig(queue_url=a.queue, region=REGION, endpoint_url=a.endpoint, wait_seconds=1),
        handler,
        visibility_timeout=30,
        codec=GlueDecodeCodec() if a.codec == "glue" else None,
    )
    task = asyncio.create_task(consumer.start())
    try:
        await asyncio.wait_for(done.wait(), a.timeout)
    except TimeoutError:
        pass
    await consumer.stop()
    await task
    links = {
        span.context.span_id: [format(link.context.trace_id, "032x") for link in span.links]
        for span in exporter.get_finished_spans()
    }
    for r in got:
        r["linked_trace_ids"] = links.get(r.pop("_span_id"), [])
    return {"language": "python", "received": got}


async def _outbox_enqueue(a: argparse.Namespace) -> dict[str, Any]:
    pool = await pgcommon.Pool.create(pgcommon.Config(dsn=a.dsn, min_conns=1, max_conns=2))
    try:
        await apply_schema(pool)
        ids, ordered = [], []
        for i in range(a.count):
            env = new_envelope(
                "interop.outbox", "python-probe", {"n": i, "s": "<&>", "u": "Zoë"},
                tenant_id="acme",
            )

            async def put(conn: Any, env: Envelope[Any] = env, i: int = i) -> None:
                if i % 2 == 0:
                    await enqueue_ordered(conn, env, a.ordering_key)
                else:
                    await enqueue(conn, env)

            await pgcommon.run_in_tx(pool, put)
            ids.append(env.id)
            if i % 2 == 0:
                ordered.append(env.id)
    finally:
        pool.close()
    return {"language": "python", "ids": ids, "ordered_ids": ordered}


async def _outbox_run(a: argparse.Namespace) -> dict[str, Any]:
    pool = await pgcommon.Pool.create(pgcommon.Config(dsn=a.dsn, min_conns=1, max_conns=4))
    try:
        publisher = SNSPublisher(SNSConfig(topic_arn=a.topic, region=REGION, endpoint_url=a.endpoint))
        runner = Runner(RunnerConfig(pool=pool, publisher=publisher, poll_interval=0.1))
        task = asyncio.create_task(runner.start())
        await asyncio.sleep(a.seconds)
        await runner.stop()
        await task
        await publisher.close()
    finally:
        pool.close()
    return {"language": "python", "ran": True}


def _topology(a: argparse.Namespace) -> dict[str, Any]:
    """An SNS topic with an SQS queue subscribed (raw message delivery), FIFO with
    --fifo; or, with --queue-only, a lone queue."""
    sns = boto3.client("sns", endpoint_url=a.endpoint, region_name=REGION)
    sqs = boto3.client("sqs", endpoint_url=a.endpoint, region_name=REGION)
    suffix = ".fifo" if a.fifo else ""
    name = f"{a.name}-{uuid.uuid4().hex[:8]}"
    qattrs = {"FifoQueue": "true"} if a.fifo else {}
    queue = sqs.create_queue(QueueName=f"{name}-q{suffix}", Attributes=qattrs)["QueueUrl"]
    if a.queue_only:
        return {"queue_url": queue}
    tattrs = {"FifoTopic": "true", "ContentBasedDeduplication": "false"} if a.fifo else {}
    topic = sns.create_topic(Name=f"{name}{suffix}", Attributes=tattrs)["TopicArn"]
    arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    sns.subscribe(
        TopicArn=topic, Protocol="sqs", Endpoint=arn, Attributes={"RawMessageDelivery": "true"}
    )
    return {"topic_arn": topic, "queue_url": queue}


def _send_raw(a: argparse.Namespace) -> dict[str, Any]:
    """Sends every body of events_raw_bodies.json, tagged with its case name."""
    sqs = boto3.client("sqs", endpoint_url=a.endpoint, region_name=REGION)
    with open(a.bodies, encoding="utf-8") as f:
        bodies = json.load(f)
    for case in bodies:
        sqs.send_message(
            QueueUrl=a.queue,
            MessageBody=case["body"],
            MessageAttributes={"Case": {"DataType": "String", "StringValue": case["case"]}},
        )
    return {"language": "python", "sent": len(bodies)}


def _seconds(value: str) -> float:
    """A duration in seconds, Go style (`60s`) or bare (`60`), as go-probe's flags take."""
    return float(value.removesuffix("s"))


def _parser(command: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"probe {command}")
    p.add_argument("--endpoint", default="http://localhost:54566")
    p.add_argument("--topic")
    p.add_argument("--queue")
    p.add_argument("--cases")
    p.add_argument("--bodies")
    p.add_argument("--mode", default="single")
    p.add_argument("--codec", default="none")
    p.add_argument("--hmac", default="")
    p.add_argument("--fifo", action="store_true")
    p.add_argument("--queue-only", action="store_true")
    p.add_argument("--name", default="interop")
    p.add_argument("--n", type=int, default=0)
    p.add_argument("--timeout", type=_seconds, default=60.0)
    p.add_argument("--dsn")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--ordering-key", default="order/py")
    p.add_argument("--for", dest="seconds", type=_seconds, default=4.0)
    return p


_ASYNC = {
    "events-publish": _publish,
    "events-consume": _consume,
    "events-outbox-enqueue": _outbox_enqueue,
    "events-outbox-run": _outbox_run,
}
_SYNC = {"events-topology": _topology, "events-send-raw": _send_raw}


def run(command: str, args: list[str]) -> int:
    a = _parser(command).parse_args(args)
    if command in _ASYNC:
        print_json(asyncio.run(_ASYNC[command](a)))
    elif command in _SYNC:
        print_json(_SYNC[command](a))
    else:
        print(f"unknown events subcommand {command!r}", file=sys.stderr)
        return 2
    return 0
