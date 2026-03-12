#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import DEFAULT_CONFIG
from core.query_ops import handle_query



def main() -> int:
    parser = argparse.ArgumentParser(description='Recruiter chat assistant helper')
    parser.add_argument('query', help='Natural language recruiter query')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    result = handle_query(args.query, config_path=Path(args.config))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result['reply'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
