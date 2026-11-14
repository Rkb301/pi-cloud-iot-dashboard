"""Raspberry Pi local dashboard and Adafruit IO MQTT gateway.

Pins are BCM-numbered. Hardware mode requires Raspberry Pi GPIO access.
Use MOCK_GPIO=1 ENABLE_MQTT=0 to preview the dashboard without a Pi.
"""
from __future__ import annotations

import logging
import os
import fcntl
import urllib.parse
import urllib.request
import urllib.error
import threading
import time
from collections import deque
from datetime import datetime, timezone
import math
from pathlib import Path

os.environ.setdefault('GPIOZERO_PIN_FACTORY', 'lgpio')


LOG = logging.getLogger('pi-iot')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, '1' if default else '0').strip().lower() in ('1', 'true', 'yes', 'on')


MOCK = env_bool('MOCK_GPIO', True)
CLOUD_ENABLED = env_bool('ENABLE_MQTT', False)
AIO_USER = (os.getenv('ADAFRUIT_IO_USERNAME') or os.getenv('AIO_USERNAME', '')).strip()
AIO_KEY = (os.getenv('ADAFRUIT_IO_KEY') or os.getenv('AIO_KEY', '')).strip()
THINGSPEAK_KEY = os.getenv('THINGSPEAK_WRITE_API_KEY', '').strip()
THINGSPEAK_ENABLED = env_bool('ENABLE_THINGSPEAK', False)
THINGSPEAK_INTERVAL = max(20, int(os.getenv('THINGSPEAK_INTERVAL_SECONDS', '30')))
ALLOW_MOCK_CLOUD = env_bool('ALLOW_MOCK_CLOUD', False)
if MOCK and not ALLOW_MOCK_CLOUD:
    CLOUD_ENABLED = False
    THINGSPEAK_ENABLED = False
TOKEN = os.getenv('DASHBOARD_TOKEN', '').strip()
WEB_PORT = int(os.getenv('WEB_PORT', '5000'))
WEB_HOST = os.getenv('WEB_HOST', '127.0.0.1').strip()
IR_ACTIVE_LOW = env_bool('IR_ACTIVE_LOW', True)
FLAME_ACTIVE_LOW = env_bool('FLAME_ACTIVE_LOW', True)
PIN_IR = int(os.getenv('PIN_IR', '17'))
PIN_FLAME = int(os.getenv('PIN_FLAME', '27'))
PIN_DHT = int(os.getenv('PIN_DHT', '22'))
PIN_LED1 = int(os.getenv('PIN_LED1', '5'))
PIN_LED2 = int(os.getenv('PIN_LED2', '6'))
PIN_LED2_MIRROR = os.getenv('PIN_LED2_MIRROR', '').strip()
PIN_LED2_MIRROR = int(PIN_LED2_MIRROR) if PIN_LED2_MIRROR else None
MQTT_BUDGET = min(24, max(2, int(os.getenv('MQTT_POINTS_PER_MINUTE', '20'))))
DHT_INTERVAL = max(2.5, float(os.getenv('DHT_INTERVAL_SECONDS', '3')))
# Only enable the GPIO4 mirror if that third physical LED wire really exists.
FEEDS = ('temperature', 'humidity', 'ir-detected', 'flame-detected', 'led1', 'led2')


def validate_pins() -> None:
    pins = {'DHT22': PIN_DHT, 'IR': PIN_IR, 'Flame': PIN_FLAME,
            'LED1': PIN_LED1, 'LED2': PIN_LED2}
    if PIN_LED2_MIRROR is not None:
        pins['LED2 mirror'] = PIN_LED2_MIRROR
    for label, pin in pins.items():
        if pin in (18, 23, 24):
            raise ValueError(f'{label}: BCM {pin} reserved for disabled legacy hardware')
        if pin not in range(2, 28):
            raise ValueError(f'{label}: invalid/unsafe BCM pin {pin}; inspect wiring')
    if len(set(pins.values())) != len(pins):
        raise ValueError(f'Conflicting BCM GPIO assignments: {pins}')
    LOG.info('Active BCM map: %s (physical header: DHT=%s IR=%s Flame=%s LED1=%s LED2=%s)',
             pins, *(BCM_PHYSICAL.get(p, '?') for p in
                     (PIN_DHT, PIN_IR, PIN_FLAME, PIN_LED1, PIN_LED2)))


BCM_PHYSICAL = {4: 7, 5: 29, 6: 31, 17: 11, 22: 15, 27: 13, 23: 16, 24: 18, 18: 12}


def pi_model() -> str:
    try:
        return Path('/sys/firmware/devicetree/base/model').read_text().rstrip('\x00')
    except OSError:
        return ''


def require_hardware_approval() -> None:
    if 'Raspberry Pi' not in pi_model():
        raise RuntimeError('Hardware mode requires the physical Raspberry Pi; use MOCK_GPIO=1 here')
    if not env_bool('HARDWARE_CONFIRMED'):
        raise RuntimeError('Verify wiring and approve activation, then set HARDWARE_CONFIRMED=1')
    if not list(Path('/dev').glob('gpiochip*')):
        raise RuntimeError('No GPIO chip devices available')


def stale(stamp: str | None, seconds: float) -> bool:
    return not stamp or (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() > seconds


class FakeLED:
    def __init__(self) -> None:
        self.is_lit = False

    def on(self) -> None:
        self.is_lit = True

    def off(self) -> None:
        self.is_lit = False

    def close(self) -> None:
        pass


class Hardware:
    def __init__(self) -> None:
        self.dht = None
        self.led2_mirror = None
        if MOCK:
            self.leds = {'led1': FakeLED(), 'led2': FakeLED()}
            self.ir = None
            self.flame = None
            LOG.info('MOCK GPIO enabled: no hardware access')
            return
        require_hardware_approval()
        from gpiozero import LED, DigitalInputDevice
        import adafruit_dht
        import board

        self.leds = {}
        self.ir = self.flame = None
        try:
            self.leds['led1'] = LED(PIN_LED1, initial_value=False)
            self.leds['led2'] = LED(PIN_LED2, initial_value=False)
            if PIN_LED2_MIRROR is not None:
                self.led2_mirror = LED(PIN_LED2_MIRROR, initial_value=False)
            self.ir = DigitalInputDevice(PIN_IR, pull_up=None, active_state=True)
            self.flame = DigitalInputDevice(PIN_FLAME, pull_up=None, active_state=True)
            self.dht = adafruit_dht.DHT22(getattr(board, f'D{PIN_DHT}'), use_pulseio=False)
        except Exception:
            self.close()
            raise

    def inputs(self) -> tuple[bool, bool]:
        if MOCK:
            return False, False
        raw_ir = bool(self.ir.value)
        raw_flame = bool(self.flame.value)
        return (not raw_ir if IR_ACTIVE_LOW else raw_ir,
                not raw_flame if FLAME_ACTIVE_LOW else raw_flame)

    def environment(self) -> tuple[float, float]:
        if MOCK:
            return 24.0, 48.0
        # DHT sensors commonly fail individual reads: caller retries on next cycle.
        t = float(self.dht.temperature)
        h = float(self.dht.humidity)
        if not math.isfinite(t) or not math.isfinite(h) or not (-40 <= t <= 80) or not (0 <= h <= 100):
            raise ValueError('DHT22 returned invalid measurement')
        return t, h

    def close(self) -> None:
        # Attempt every release even if one device fails during shutdown.
        for led in [*self.leds.values(), self.led2_mirror]:
            if led is not None:
                for action in (led.off, led.close):
                    try:
                        action()
                    except Exception as exc:
                        LOG.warning('LED cleanup failed: %s', type(exc).__name__)
        for device in (self.ir, self.flame, self.dht):
            if device is not None:
                try:
                    (device.exit if device is self.dht else device.close)()
                except Exception as exc:
                    LOG.warning('Sensor cleanup failed: %s', type(exc).__name__)


class IoTServer:
    def __init__(self) -> None:
        validate_pins()
        self.instance_lock = None
        if not MOCK:
            require_hardware_approval()
            self.instance_lock = Path(__file__).with_name('.hardware.lock').open('a')
            try:
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.instance_lock.close()
                raise RuntimeError('Another instance owns this project GPIO lock') from None
        try:
            self.hw = Hardware()
        except Exception:
            if self.instance_lock:
                self.instance_lock.close()
            raise
        self.lock = threading.RLock()
        self.started = datetime.now(timezone.utc).isoformat()
        self.state = {
            'temperature': None, 'humidity': None,
            'ir_detected': None, 'flame_detected': None,
            'led1': False, 'led2': False,
            'dht_updated_at': None, 'inputs_updated_at': None,
            'dht_error': None, 'inputs_error': None,
        }
        self.mqtt_connected = False
        self.mqtt_client = None
        self.publish_history: deque[float] = deque()
        self.echoes = {'led1': deque(maxlen=48), 'led2': deque(maxlen=48)}
        self.dirty_leds = set()
        self.cloud_pause_until = 0.0
        self.mqtt_status = 'disabled' if not CLOUD_ENABLED else 'waiting'
        self.thingspeak_last_entry_id = None
        self.ts_last_attempt = None
        self.last_event_publish = {'ir-detected': 0.0, 'flame-detected': 0.0}
        self.thingspeak_status = 'disabled' if not THINGSPEAK_ENABLED else 'waiting'
        self.thingspeak_last_success_at = None
        self.stopping = threading.Event()
        self.ts_worker = threading.Thread(target=self._thingspeak_loop, name='thingspeak-writer', daemon=True)
        self.worker = threading.Thread(target=self._read_loop, name='sensor-poller', daemon=True)

    def get_state(self) -> dict:
        with self.lock:
            data = dict(self.state)
            data.update({
                'mqtt_connected': self.mqtt_connected,
                'mqtt_status': self.mqtt_status,
                'mqtt_pending_leds': sorted(self.dirty_leds) if CLOUD_ENABLED else [],
                'thingspeak_last_entry_id': self.thingspeak_last_entry_id,
                'thingspeak_enabled': THINGSPEAK_ENABLED and bool(THINGSPEAK_KEY),
                'thingspeak_status': self.thingspeak_status,
                'thingspeak_last_success_at': self.thingspeak_last_success_at,
                'cloud_enabled': CLOUD_ENABLED and bool(AIO_USER and AIO_KEY),
                'mock_mode': MOCK,
                'started_at': self.started,
                'pins_bcm': {'dht': PIN_DHT, 'ir': PIN_IR, 'flame': PIN_FLAME,
                             'led1': PIN_LED1, 'led2': PIN_LED2, 'led2_mirror': PIN_LED2_MIRROR},
                'server_time': datetime.now(timezone.utc).isoformat(),
            })
        data['dht_stale'] = stale(data['dht_updated_at'], 120)
        data['inputs_stale'] = stale(data['inputs_updated_at'], 5) or bool(data['inputs_error'])
        return data

    def set_led(self, name: str, enabled: bool, *, from_cloud: bool = False) -> None:
        if type(enabled) is not bool:
            raise ValueError('LED command must be boolean')
        if name not in ('led1', 'led2'):
            raise ValueError('Unknown LED')
        with self.lock:
            if self.stopping.is_set():
                raise RuntimeError('Gateway is stopping')
            changed = self.state[name] != enabled
            try:
                (self.hw.leds[name].on if enabled else self.hw.leds[name].off)()
                if name == 'led2' and self.hw.led2_mirror is not None:
                    (self.hw.led2_mirror.on if enabled else self.hw.led2_mirror.off)()
            finally:
                self.state[name] = bool(self.hw.leds[name].is_lit)
                if changed or not from_cloud:
                    self.dirty_leds.add(name)
            if self.state[name] != enabled:
                raise RuntimeError('GPIO output readback differs from command')
        LOG.info('%s -> %s (%s)', name, 'ON' if enabled else 'OFF', 'cloud' if from_cloud else 'local')
        self.flush_leds()

    def flush_leds(self) -> None:
        with self.lock:
            for feed in sorted(self.dirty_leds):
                if self.publish(feed, int(self.state[feed])):
                    self.dirty_leds.discard(feed)

    def publish(self, feed: str, value: str | int | float) -> bool:
        # Free-tier Adafruit IO allows 30 data points/minute. Keep headroom for manual toggles.
        with self.lock:
            if self.stopping.is_set() or not self.mqtt_connected or self.mqtt_client is None:
                return False
            now = time.monotonic()
            if now < self.cloud_pause_until:
                return False
            while self.publish_history and now - self.publish_history[0] >= 60:
                self.publish_history.popleft()
            if len(self.publish_history) >= MQTT_BUDGET:
                self.mqtt_status = 'rate limited (local budget)'
                return False
            self.publish_history.append(now)
            client = self.mqtt_client
            try:
                result = client.publish(f'{AIO_USER}/feeds/{feed}', str(value), qos=0)
                if result.rc != 0:
                    self.mqtt_status = 'publish failed'
                    return False
                if feed in self.echoes:
                    self.echoes[feed].append((now, str(value)))
                self.mqtt_status = 'connected (publish queued)'
                return True
            except Exception as exc:
                self.mqtt_status = 'publish failed'
                LOG.warning('MQTT publish failed: %s', type(exc).__name__)
                return False

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            self.mqtt_connected = False
            self.mqtt_status = 'connection rejected'
            LOG.error('MQTT connect rejected: %s', reason_code)
            return
        with self.lock:
            self.mqtt_connected = True
            self.mqtt_status = 'connected'
            for echoes in self.echoes.values():
                echoes.clear()
        LOG.info('Connected to Adafruit IO over TLS')
        topics = [f'{AIO_USER}/feeds/{feed}' for feed in ('led1', 'led2')]
        topics += [f'{AIO_USER}/throttle', f'{AIO_USER}/errors']
        for topic in topics:
            result, _ = client.subscribe(topic, qos=0)
            if result != 0:
                self.mqtt_status = 'subscription failed'
                client.disconnect()
                return
        # Boot safety: LEDs start OFF and cloud toggles are reset to reflect actual hardware.
        # On an ordinary reconnect, publish the current local state instead.
        with self.lock:
            self.dirty_leds.update(('led1', 'led2'))
        self.flush_leds()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        with self.lock:
            self.mqtt_connected = False
            self.mqtt_status = 'disconnected'
        LOG.warning('Adafruit IO disconnected: %s', reason_code)

    def _on_message(self, client, userdata, message) -> None:
        if message.topic in (f'{AIO_USER}/throttle', f'{AIO_USER}/errors'):
            with self.lock:
                self.mqtt_status = 'broker throttle/error; waiting 60s'
                self.cloud_pause_until = time.monotonic() + 60
                self.dirty_leds.update(('led1', 'led2'))
            return
        feed = message.topic.rsplit('/', 1)[-1]
        if message.topic != f'{AIO_USER}/feeds/{feed}' or feed not in ('led1', 'led2'):
            return
        if getattr(message, 'retain', False) or len(message.payload) > 16:
            return
        try:
            raw = message.payload.decode('utf-8').strip().lower()
        except UnicodeDecodeError:
            return
        if raw not in ('1', 'on', 'true', '0', 'off', 'false'):
            LOG.warning('Rejected invalid %s command', feed)
            return
        with self.lock:
            echoes = self.echoes[feed]
            while echoes and time.monotonic() - echoes[0][0] > 60:
                echoes.popleft()
            if echoes and echoes[0][1] == raw:
                echoes.popleft()
                return
            try:
                self.set_led(feed, raw in ('1', 'on', 'true'), from_cloud=True)
            except Exception as exc:
                self.mqtt_status = 'output command failed'
                LOG.warning('Cloud output command failed: %s', type(exc).__name__)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if any(code.is_failure for code in reason_codes):
            with self.lock:
                self.mqtt_status = 'subscription rejected'
            client.disconnect()

    def start(self) -> None:
        if CLOUD_ENABLED:
            if not AIO_USER or not AIO_KEY:
                self.mqtt_status = 'missing credentials'
                LOG.warning('Cloud is disabled: configure AIO_USERNAME and AIO_KEY in .env')
            else:
                import paho.mqtt.client as mqtt
                self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
                self.mqtt_client.username_pw_set(AIO_USER, AIO_KEY)
                self.mqtt_client.tls_set()  # Validates io.adafruit.com's certificate.
                self.mqtt_client.on_connect = self._on_connect
                self.mqtt_client.on_disconnect = self._on_disconnect
                self.mqtt_client.on_message = self._on_message
                self.mqtt_client.on_subscribe = self._on_subscribe
                self.mqtt_client.reconnect_delay_set(min_delay=2, max_delay=60)
                self.mqtt_client.connect_async('io.adafruit.com', port=8883, keepalive=60)
                self.mqtt_client.loop_start()
        self.worker.start()
        if THINGSPEAK_ENABLED:
            if not THINGSPEAK_KEY:
                LOG.warning('ThingSpeak disabled: missing THINGSPEAK_WRITE_API_KEY')
                with self.lock:
                    self.thingspeak_status = 'missing API key'
            else:
                self.ts_worker.start()

    def _thingspeak_once(self) -> bool:
        if not THINGSPEAK_ENABLED or not THINGSPEAK_KEY or self.stopping.is_set():
            return False
        with self.lock:
            now = time.monotonic()
            if self.ts_last_attempt is not None and now - self.ts_last_attempt < THINGSPEAK_INTERVAL:
                return False
            d = self.get_state()
            if d['dht_stale'] or d['inputs_stale']:
                self.thingspeak_status = 'awaiting fresh sensors'
                return False
            self.ts_last_attempt = now
        payload = {'api_key': THINGSPEAK_KEY}
        for index, key in enumerate(('temperature', 'humidity', 'ir_detected',
                                     'flame_detected', 'led1', 'led2'), 1):
            value = d[key]
            payload[f'field{index}'] = str(int(value) if isinstance(value, bool) else value)
        try:
            req = urllib.request.Request(
                'https://api.thingspeak.com/update',
                data=urllib.parse.urlencode(payload).encode('ascii'),
                headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
            with urllib.request.urlopen(req, timeout=8) as response:
                result = response.read(32).decode('ascii', errors='replace').strip()
            if not result.isdigit() or int(result) <= 0:
                raise ValueError('Rejected update')
            with self.lock:
                self.thingspeak_status = 'last upload succeeded'
                self.thingspeak_last_entry_id = int(result)
                self.thingspeak_last_success_at = datetime.now(timezone.utc).isoformat()
            return True
        except (OSError, ValueError) as exc:
            with self.lock:
                self.thingspeak_status = 'error'
            LOG.warning('ThingSpeak update unsuccessful: %s', type(exc).__name__)
            return False

    def _thingspeak_loop(self) -> None:
        """HTTPS never blocks the GPIO poller; failed attempts also obey the interval."""
        while not self.stopping.is_set():
            self._thingspeak_once()
            self.stopping.wait(THINGSPEAK_INTERVAL)

    def read_dht(self) -> None:
        try:
            temp, humidity = self.hw.environment()
            if not math.isfinite(temp) or not math.isfinite(humidity) or not -40 <= temp <= 80 or not 0 <= humidity <= 100:
                raise ValueError('Invalid DHT22 sample')
            with self.lock:
                self.state.update(temperature=round(temp, 1), humidity=round(humidity, 1),
                                  dht_updated_at=datetime.now(timezone.utc).isoformat(), dht_error=None)
        except Exception as exc:
            with self.lock:
                self.state['dht_error'] = type(exc).__name__
            LOG.warning('DHT read failed; retrying: %s', type(exc).__name__)

    def _read_loop(self) -> None:
        last_dht = float('-inf')
        last_snapshot = float('-inf')
        first = True
        while not self.stopping.is_set():
            now = time.monotonic()
            try:
                ir, flame = self.hw.inputs()
                stamp = datetime.now(timezone.utc).isoformat()
                with self.lock:
                    old_ir = self.state['ir_detected']
                    old_flame = self.state['flame_detected']
                    self.state['ir_detected'] = ir
                    self.state['flame_detected'] = flame
                    self.state['inputs_updated_at'] = stamp
                    self.state['inputs_error'] = None
                for feed, changed, value in (
                    ('ir-detected', first or ir != old_ir, ir),
                    ('flame-detected', first or flame != old_flame, flame),
                ):
                    # Publish edges (up to once every 4 s per sensor), plus periodic snapshots.
                    if changed and (first or now - self.last_event_publish[feed] >= 4):
                        if self.publish(feed, int(value)):
                            self.last_event_publish[feed] = now
                first = False
            except Exception as exc:
                with self.lock:
                    self.state['inputs_error'] = type(exc).__name__
                LOG.warning('Digital input read failed: %s', type(exc).__name__)

            if now - last_dht >= DHT_INTERVAL:
                last_dht = now
                self.read_dht()
            self.flush_leds()

            if now - last_snapshot >= 30:
                last_snapshot = now
                with self.lock:
                    self.dirty_leds.update(('led1', 'led2'))
                self.flush_leds()
                snapshot = self.get_state()
                with self.lock:
                    values = [(feed, snapshot[key]) for feed, key in (
                        ('temperature', 'temperature'), ('humidity', 'humidity'),
                        ('ir-detected', 'ir_detected'), ('flame-detected', 'flame_detected')
                    )]
                for feed, value in values:
                    is_stale = snapshot['dht_stale'] if feed in ('temperature', 'humidity') else snapshot['inputs_stale']
                    if value is not None and not is_stale:
                        self.publish(feed, int(value) if isinstance(value, bool) else value)
            self.stopping.wait(0.5)

    def stop(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self.worker.is_alive():
            self.worker.join(timeout=10)
        if self.ts_worker.is_alive():
            self.ts_worker.join(timeout=9)
        try:
            if self.mqtt_client is not None:
                try:
                    self.mqtt_client.disconnect()
                finally:
                    self.mqtt_client.loop_stop()
        finally:
            with self.lock:
                self.hw.close()
            if self.instance_lock:
                self.instance_lock.close()
