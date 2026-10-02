"""``python -m app.worker`` - the live ingest worker.

    python -m app.worker                 run until Ctrl+C / SIGTERM
    python -m app.worker --once          run each job once and exit (no stream)
    python -m app.worker --no-stream     scheduler only, without the force-order websocket
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence

from app.worker.scheduler import Worker, build_context


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m app.worker', description=__doc__.strip().splitlines()[0])
    parser.add_argument('--once', action='store_true', help='run every job once, print the outcome, exit')
    parser.add_argument('--no-stream', action='store_true', help='do not start the force-order websocket')
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%SZ',
    )
    logging.Formatter.converter = time.gmtime  # timestamps in UTC, like everything else

    ctx = build_context()
    worker = Worker(ctx, with_stream=not (args.no_stream or args.once))
    if args.once:
        results = worker.run_once()
        for name, status in results.items():
            print(f'{name:28s} {status}')
        worker.ctx.client.close()
        return 0 if all(status in ('ok', 'skipped') for status in results.values()) else 2
    worker.run_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
