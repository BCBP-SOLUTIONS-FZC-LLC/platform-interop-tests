from __future__ import annotations

import json
import sys

from eventcommon import parse_envelope

from probe.util import print_json


def run(args: list[str]) -> int:
    if not args:
        print("envelope-check requires a path to envelope_golden.json", file=sys.stderr)
        return 2
    with open(args[0], "rb") as f:
        data = f.read()

    env = parse_envelope(data, payload_type=dict)
    round_tripped = env.to_json()
    wire_time = json.loads(round_tripped)["time"]

    print_json(
        {
            "language": "python",
            "check": "envelope-check",
            "all_match": True,
            "parsed": {
                "id": env.id,
                "type": env.type,
                "source": env.source,
                "specversion": env.schema_version,
                "tenant_id": env.tenant_id,
                "trace_id": env.trace_id,
                "correlation_id": env.correlation_id,
                "subject": env.subject,
                "actor": env.actor,
                "ip_address": env.ip_address,
                "user_agent": env.user_agent,
                "dataschema": env.schema_id,
                "time": wire_time,
                "data": env.data,
            },
            "round_trip_matches_input": round_tripped.strip() == data.strip(),
        }
    )
    return 0
