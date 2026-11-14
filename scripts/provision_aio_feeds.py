#!/usr/bin/env python3
"""Optional Adafruit IO feed provisioning. Default is read-only plan (no API calls)."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / '.env')
FEEDS = ('led1', 'led2', 'temperature', 'humidity', 'ir-detected', 'flame-detected')


def call(url: str, key: str, method: str = 'GET', payload: dict | None = None) -> dict:
    headers = {'X-AIO-Key': key, 'Accept': 'application/json'}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = Request(url, data=body, method=method, headers=headers)
    with urlopen(req, timeout=15) as resp:
        return json.load(resp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Explicitly allow creation of missing feeds in your Adafruit account')
    args = parser.parse_args()
    username = (os.getenv('ADAFRUIT_IO_USERNAME') or os.getenv('AIO_USERNAME', '')).strip()
    key = (os.getenv('ADAFRUIT_IO_KEY') or os.getenv('AIO_KEY', '')).strip()
    print('Planned Adafruit IO feeds:', ', '.join(FEEDS))
    if not args.apply:
        print('Dry run only. No network access. Use --apply to check/create missing feeds.')
        return
    if not username or not key:
        raise SystemExit('Adafruit IO credentials missing from private .env')
    base = f'https://io.adafruit.com/api/v2/{quote(username, safe="")}/feeds'
    for feed in FEEDS:
        try:
            call(base + '/' + quote(feed, safe=''), key)
            print('exists:', feed)
        except HTTPError as exc:
            if exc.code != 404:
                print('HTTP failure when checking:', feed, 'status', exc.code)
                raise SystemExit(2) from None
            try:
                created = call(base, key, 'POST', {'feed': {'name': feed}})
                print('created:', created.get('key', feed))
            except HTTPError as create_exc:
                print('Could not create:', feed, 'HTTP status', create_exc.code)
                raise SystemExit(2) from None


if __name__ == '__main__':
    main()
