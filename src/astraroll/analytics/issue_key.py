"""Generate one expiring analytics API key and its server-side digest record.

Run locally: python -m astraroll.analytics.issue_key --subject demo --tenant UUID
The secret is shown once on stdout; never paste it into tracked files or logs.
"""

import argparse
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--tenant", type=UUID, required=True, action="append")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    if not args.subject.strip() or len(args.subject.strip()) > 120 or not 1 <= args.hours <= 720:
        parser.error("Use a nonempty subject up to 120 characters and expiry 1..720 hours")
    token = secrets.token_urlsafe(32)
    record = {
        "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "subject": args.subject.strip(),
        "tenant_ids": [str(t) for t in args.tenant],
        "expires_at": (datetime.now(UTC) + timedelta(hours=args.hours)).isoformat(),
    }
    print("Bearer key (keep secret): " + token)
    print("Server registry record (add to ANALYTICS_API_KEYS_JSON array):")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
