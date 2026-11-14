#!/usr/bin/env python3
"""Read-only host/dependency checks. --cloud checks TLS, feed existence and MQTT auth; never publishes."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FEEDS = ('led1', 'led2', 'temperature', 'humidity', 'ir-detected', 'flame-detected')


def tls_check(host, port):
    try:
        with socket.create_connection((host, port), timeout=8) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as conn:
                return {'ok': True, 'tls': conn.version()}
    except OSError as exc:
        return {'ok': False, 'error_type': type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud', action='store_true')
    args = parser.parse_args()
    model_path = Path('/sys/firmware/devicetree/base/model')
    model = model_path.read_text().rstrip('\0') if model_path.exists() else 'not detected'
    packages = {}
    for name in ('Flask', 'python-dotenv', 'paho-mqtt', 'waitress', 'gpiozero', 'lgpio', 'Adafruit-Blinka', 'adafruit-circuitpython-dht'):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = 'not installed'
    env_path = ROOT / '.env'
    result = dict(machine=platform.machine(), python=platform.python_version(), board=model,
                  raspberry_pi='Raspberry Pi' in model, gpio_devices=[p.name for p in Path('/dev').glob('gpiochip*')],
                  dependencies=packages, env_present=env_path.exists(),
                  env_private=env_path.exists() and env_path.stat().st_mode & 0o077 == 0)
    if args.cloud:
        result['tls'] = {f'{host}:{port}': tls_check(host, port) for host, port in
                         (('io.adafruit.com', 8883), ('api.thingspeak.com', 443))}
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            result['authenticated_checks'] = 'blocked: install requirements.txt'
        else:
            user = os.getenv('ADAFRUIT_IO_USERNAME') or os.getenv('AIO_USERNAME', '')
            key = os.getenv('ADAFRUIT_IO_KEY') or os.getenv('AIO_KEY', '')
            result['feeds'] = {}
            if user and key:
                for feed in FEEDS:
                    url = 'https://io.adafruit.com/api/v2/' + urllib.parse.quote(user, safe='') + '/feeds/' + feed
                    req = urllib.request.Request(url, headers={'X-AIO-Key': key})
                    try:
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            result['feeds'][feed] = 'exists' if resp.status == 200 else 'unexpected status'
                    except urllib.error.HTTPError as exc:
                        result['feeds'][feed] = 'missing' if exc.code == 404 else f'HTTP {exc.code}'
                    except OSError as exc:
                        result['feeds'][feed] = type(exc).__name__
                try:
                    import paho.mqtt.client as mqtt
                    done = threading.Event()
                    result['mqtt_auth'] = 'timeout'
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                    client.username_pw_set(user, key)
                    client.tls_set()
                    def connected(client, userdata, flags, reason, properties):
                        result['mqtt_auth'] = 'accepted' if reason == 0 else 'rejected'
                        done.set()
                    client.on_connect = connected
                    client.connect_async('io.adafruit.com', 8883, 60)
                    client.loop_start()
                    try: done.wait(10)
                    finally:
                        client.disconnect()
                        client.loop_stop()
                except (ImportError, OSError) as exc:
                    result['mqtt_auth'] = type(exc).__name__
            else:
                result['mqtt_auth'] = 'credentials missing'
        result['thingspeak_write'] = 'not attempted; requires real samples or explicit sandbox-test authorization'
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
