from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage: python -m probe "
            "<gucset-check|hmac-check|hmac-sign|envelope-check|metrics-check|header-check|"
            "pg-rls-check|http-serve> [args...]",
            file=sys.stderr,
        )
        return 2

    subcommand, rest = sys.argv[1], sys.argv[2:]

    if subcommand == "gucset-check":
        from probe.gucset import run
    elif subcommand == "hmac-check":
        from probe.hmac_check import run
    elif subcommand == "hmac-sign":
        from probe.hmac_check import run_sign as run
    elif subcommand == "envelope-check":
        from probe.envelope_check import run
    elif subcommand == "metrics-check":
        from probe.metrics_check import run
    elif subcommand == "header-check":
        from probe.header_check import run
    elif subcommand == "pg-rls-check":
        from probe.pgrls_check import run
    elif subcommand == "http-serve":
        from probe.http_serve import run
    else:
        print(f"unknown subcommand {subcommand!r}", file=sys.stderr)
        return 2

    return run(rest)


if __name__ == "__main__":
    raise SystemExit(main())
