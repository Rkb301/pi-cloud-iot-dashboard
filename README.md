# Raspberry Pi Cloud IoT Dashboard

A Python/Flask IoT control and monitoring prototype for a Raspberry Pi. The application combines local sensor readings, an authenticated dashboard, two LED controls, and optional outbound integrations with Adafruit IO and ThingSpeak.

## Implemented scope

- Dashboard for DHT22 temperature/humidity, reflective IR obstacle detection and a flame-detector module
- Two LED outputs controlled locally through an authenticated dashboard
- Adafruit IO MQTT telemetry and remote control (optional, disabled in local mock mode)
- ThingSpeak HTTPS telemetry upload (optional, rate limited)
- Mock GPIO mode for testing without physical hardware
- Preflight checks, test modules and a Raspberry Pi install script

## Stack

Python 3, Flask, paho-mqtt, python-dotenv, Waitress, GPIOZero-compatible sensor access, HTML/JavaScript dashboard.

## Hardware mapping

| Device | BCM GPIO |
| --- | --- |
| DHT22 data | 22 |
| IR obstacle digital output | 17 |
| Flame module digital output | 27 |
| LED 1 (through a resistor) | 5 |
| LED 2 (through a resistor) | 6 |

**Safety:** Raspberry Pi GPIO is 3.3 V-only. Check the actual sensor module's logic voltage and wiring before connecting it. Power down before rewiring. This is a lab monitoring prototype, **not a certified safety alarm**.

## Run in safe mock mode

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set MOCK_GPIO=1, ENABLE_MQTT=0, ENABLE_THINGSPEAK=0
python app.py
```

Visit `http://127.0.0.1:5000` for the local dashboard. In mock mode, values are simulated and do not represent real sensors. The local LED control token is configured with the `DASHBOARD_TOKEN` environment variable; set a fresh value in your private `.env` if you plan to exercise controls.

## Test locally

```bash
MOCK_GPIO=1 ENABLE_MQTT=0 ENABLE_THINGSPEAK=0 ALLOW_MOCK_CLOUD=0 python -m unittest discover -s tests -v
node tests/test_dashboard.js
```

These tests are designed to run without using real GPIO or cloud credentials. Hardware-specific tests require the corresponding equipment and should not be conflated with mock-mode results.

## Raspberry Pi deployment

See `scripts/install_pi.sh` for prerequisite checks and installation. Supply credentials as environment variables or a local `.env` file **never committed to Git**. Set `HARDWARE_CONFIRMED=1` only after physically checking the GPIO and power configuration. The server defaults to loopback; do not expose a token-protected HTTP interface directly to public networks.

**Integration notes:** Adafruit IO uses outbound authenticated MQTT; ThingSpeak uses authenticated HTTPS. Cloud quotas, freshness checks and actual connection results depend on deployed accounts and network connectivity. Credentials and production telemetry are intentionally excluded.

## Repository layout

| Files | Purpose |
| --- | --- |
| `app.py`, `gateway.py` | Flask app, sensor gateway, auth, telemetry and cloud logic |
| `templates/index.html` | Responsive dashboard UI |
| `scripts/` | Pi setup, validation and feed provisioning |
| `systemd/` | Service deployment template |
| `tests/` | Mock API and client dashboard tests |
| `.env.example` | Configuration template; **no live credentials** |

## Status and limitations

- This is an educational/portfolio system demonstrated with mock-mode tests; production security and full physical hardware validation are separate tasks.
- No real cloud tokens, local passwords, personal logs, database exports or university-specific legacy reference files are included.
- Do not use the IR or flame modules as safety-critical alarms.

**Portfolio focus:** Python · IoT · Flask · Cloud MQTT/HTTPS · Raspberry Pi · embedded systems · hardware/software integration
