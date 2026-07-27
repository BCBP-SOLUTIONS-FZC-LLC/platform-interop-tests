from __future__ import annotations

import json
import sys
from typing import Any


def print_json(value: dict[str, Any]) -> None:
    # sort_keys=False deliberately: some callers (hmac_check.run_sign) embed a verbatim
    # envelope's raw "data" payload, whose exact byte-level key order is part of what gets
    # HMAC-signed. Sorting keys here would silently re-order that payload relative to what was
    # actually signed, breaking cross-language signature verification for reasons that have
    # nothing to do with the actual contract being tested. Consumers that want stable diff
    # ordering should sort at the comparison layer instead (see run_interop.sh).
    json.dump(value, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
