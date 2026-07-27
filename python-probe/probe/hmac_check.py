from __future__ import annotations

import json
import sys

from eventcommon import parse_envelope, sign_envelope, verify_envelope

from probe.util import print_json


def _load_vector(path: str) -> dict[str, object]:
    with open(path) as f:
        return json.load(f)


def run_check(args: list[str]) -> int:
    """hmac-check: reproduces the Go-computed signature already committed in the fixture."""
    if not args:
        print("hmac-check requires a path to hmac_vector.json", file=sys.stderr)
        return 2
    vector = _load_vector(args[0])
    key = bytes.fromhex(vector["key_hex"])  # type: ignore[arg-type]
    envelope_json = json.dumps(vector["envelope_json"]).encode("utf-8")
    env = parse_envelope(envelope_json, payload_type=bytes)

    python_sig = sign_envelope(key, env)
    reproduces = python_sig == vector["signature_hex"]
    verifies = verify_envelope(key, env, vector["signature_hex"])  # type: ignore[arg-type]

    print_json(
        {
            "language": "python",
            "check": "hmac-check",
            "python_signature_hex": python_sig,
            "python_reproduces_fixture_sig": reproduces,
            "verify_fixture_signature": verifies,
            "all_match": reproduces and verifies,
        }
    )
    return 0


def run_sign(args: list[str]) -> int:
    """hmac-sign: signs the given envelope fixture with Python and prints
    {key_hex, envelope_json, signature_hex} to stdout — piped into go-probe's `hmac-verify` to
    complete the bidirectional check (Python signs, Go verifies)."""
    if not args:
        print("hmac-sign requires a path to an envelope JSON fixture and a key_hex", file=sys.stderr)
        return 2
    envelope_path, key_hex = args[0], args[1]
    key = bytes.fromhex(key_hex)
    with open(envelope_path, "rb") as f:
        envelope_json = f.read()
    env = parse_envelope(envelope_json, payload_type=bytes)
    sig = sign_envelope(key, env)

    print_json({"key_hex": key_hex, "envelope_json": json.loads(envelope_json), "signature_hex": sig})
    return 0


def run(args: list[str]) -> int:
    return run_check(args)
