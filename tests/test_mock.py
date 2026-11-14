"""Deterministic GPIO/MQTT/HTTP integration tests; never contact real cloud services."""
import os
os.environ.update(MOCK_GPIO='1', ENABLE_MQTT='0', ENABLE_THINGSPEAK='0',
                  ALLOW_MOCK_CLOUD='0', DASHBOARD_TOKEN='test-demo-secret',
                  ADAFRUIT_IO_USERNAME='test-user', ADAFRUIT_IO_KEY='test-aio-key',
                  THINGSPEAK_WRITE_API_KEY='test-ts-key', PIN_DHT='22', PIN_IR='17',
                  PIN_FLAME='27', PIN_LED1='5', PIN_LED2='6', PIN_LED2_MIRROR='')
import time
import threading
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.parse
import gateway as app


def stamp(age=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.s = app.IoTServer()
        self.cloud = Mock()
        self.cloud.publish.return_value = SimpleNamespace(rc=0)
        self.cloud.subscribe.return_value = (0, 1)
        self.s.mqtt_client = self.cloud
        # Any unintended real network use fails immediately.
        self.net = patch('gateway.urllib.request.urlopen', side_effect=AssertionError('Unexpected network'))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.addCleanup(self.s.stop)

    def fresh(self):
        self.s.state.update(temperature=24.0, humidity=48.0, ir_detected=True,
                            flame_detected=False, inputs_updated_at=stamp(), dht_updated_at=stamp())

    def message(self, payload, feed='led1', topic=None, retain=False):
        self.s._on_message(self.cloud, None, SimpleNamespace(
            payload=payload, topic=topic or f'test-user/feeds/{feed}', retain=retain))





    def test_pin_conflict_range_and_disabled_legacy_outputs(self):
        for pin in (6, 1, 28, 18, 23, 24):
            with self.subTest(pin=pin), patch.object(app,'PIN_LED1',pin):
                with self.assertRaises(ValueError): app.validate_pins()
        with patch.object(app,'PIN_LED2_MIRROR',5):
            with self.assertRaises(ValueError): app.validate_pins()

    def test_hardware_guard_rejects_laptop_and_unapproved_pi(self):
        with patch.object(app,'pi_model',return_value='Laptop'):
            with self.assertRaisesRegex(RuntimeError,'physical Raspberry Pi'): app.require_hardware_approval()
        with patch.object(app,'pi_model',return_value='Raspberry Pi 5'), patch.dict(os.environ,HARDWARE_CONFIRMED='0'):
            with self.assertRaisesRegex(RuntimeError,'approve'): app.require_hardware_approval()

    def test_dht_failure_preserves_timestamp_then_recovers(self):
        self.fresh(); self.s.state['dht_updated_at']=stamp(121)
        old=self.s.state['dht_updated_at']
        with patch.object(self.s.hw,'environment',side_effect=RuntimeError('checksum')):
            self.s.read_dht()
        self.assertEqual(self.s.state['dht_updated_at'],old)
        self.assertEqual(self.s.state['temperature'],24)
        self.assertTrue(self.s.get_state()['dht_stale'])
        self.s.read_dht()
        self.assertFalse(self.s.get_state()['dht_stale'])
        self.assertIsNone(self.s.state['dht_error'])

    def test_invalid_dht_values_never_become_samples(self):
        for values in ((float('nan'),40),(22,101),(81,40)):
            with patch.object(self.s.hw,'environment',return_value=values): self.s.read_dht()
            self.assertIsNone(self.s.state['dht_updated_at'])

    def test_stale_and_failed_inputs_are_unknown(self):
        self.fresh(); self.s.state['inputs_updated_at']=stamp(6)
        self.assertTrue(self.s.get_state()['inputs_stale'])
        self.fresh(); self.s.state['inputs_error']='OSError'
        self.assertTrue(self.s.get_state()['inputs_stale'])

    def test_polling_and_shutdown(self):
        self.s.start()
        deadline=time.monotonic()+2
        while self.s.state['temperature'] is None and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(self.s.state['temperature'],24)
        self.assertFalse(self.s.state['flame_detected'])
        self.s.set_led('led1',True)
        self.s.stop(); self.s.stop()
        self.assertFalse(self.s.hw.leds['led1'].is_lit)
        self.assertFalse(self.s.worker.is_alive())

    def test_mqtt_payload_validation(self):
        for raw in (b'1',b'on',b' TRUE '):
            self.message(raw); self.assertTrue(self.s.state['led1'])
            self.message(b'0'); self.assertFalse(self.s.state['led1'])
        for raw in (b'toggle',b'2',b'{}',b'\xff',b'1'*100):
            self.message(raw); self.assertFalse(self.s.state['led1'])
        self.message(b'1',topic='other/feeds/led1')
        self.message(b'1',retain=True)
        self.assertFalse(self.s.state['led1'])

    def test_mqtt_reconnect_publishes_current_outputs(self):
        self.s.set_led('led1',True)
        self.s._on_connect(self.cloud,None,None,0,None)
        self.assertTrue(self.s.mqtt_connected)
        self.cloud.subscribe.assert_any_call('test-user/feeds/led2',qos=0)
        self.cloud.publish.assert_any_call('test-user/feeds/led1','1',qos=0)
        self.s._on_disconnect(self.cloud,None,None,1,None)
        self.assertFalse(self.s.mqtt_connected)
        self.s._on_connect(self.cloud,None,None,5,None)
        self.assertFalse(self.s.mqtt_connected)

    def test_mqtt_echo_does_not_revert_new_local_command(self):
        self.s.mqtt_connected=True
        self.s.set_led('led1',True)
        self.s.set_led('led1',False)
        self.message(b'1'); self.message(b'0')
        self.assertFalse(self.s.state['led1'])
        self.assertEqual(self.cloud.publish.call_count,2)

    def test_mqtt_cloud_command_reports_output_once(self):
        self.s.mqtt_connected=True
        self.message(b'ON')
        self.assertTrue(self.s.hw.leds['led1'].is_lit)
        self.message(b'1')
        self.assertEqual(self.cloud.publish.call_count,1)

    def test_mqtt_budget_keeps_latest_output_for_retry(self):
        self.s.mqtt_connected=True
        self.s.publish_history.extend([time.monotonic()]*app.MQTT_BUDGET)
        self.s.set_led('led1',True); self.s.set_led('led1',False)
        self.assertIn('led1',self.s.dirty_leds)
        self.cloud.publish.assert_not_called()
        self.s.publish_history.clear(); self.s.flush_leds()
        self.cloud.publish.assert_called_once_with('test-user/feeds/led1','0',qos=0)
        self.assertFalse(self.s.dirty_leds)

    def test_mqtt_publish_failure_is_pending(self):
        self.s.mqtt_connected=True
        self.cloud.publish.return_value=SimpleNamespace(rc=4)
        self.s.set_led('led1',True)
        self.assertIn('led1',self.s.dirty_leds)
        self.assertEqual(self.s.mqtt_status,'publish failed')

    def test_mqtt_tls_setup_and_backoff(self):
        try:
            import paho.mqtt.client
        except ImportError:
            self.skipTest('paho-mqtt not installed')
        with patch.object(app,'CLOUD_ENABLED',True), patch('paho.mqtt.client.Client',return_value=self.cloud):
            self.s.start()
        self.cloud.tls_set.assert_called_once_with()
        self.cloud.connect_async.assert_called_once_with('io.adafruit.com',port=8883,keepalive=60)
        self.cloud.reconnect_delay_set.assert_called_once_with(min_delay=2,max_delay=60)

    def test_thingspeak_field_mapping_https_body_and_rate(self):
        self.fresh()
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        response.read.return_value=b'1234'
        with patch.object(app,'THINGSPEAK_ENABLED',True), patch('gateway.urllib.request.urlopen',return_value=response) as http:
            self.assertTrue(self.s._thingspeak_once())
            self.assertFalse(self.s._thingspeak_once())
            http.assert_called_once()
            req=http.call_args.args[0]
            self.assertEqual(req.full_url,'https://api.thingspeak.com/update')
            self.assertEqual(req.method,'POST')
            self.assertEqual(urllib.parse.parse_qs(req.data.decode()),dict(api_key=['test-ts-key'],field1=['24.0'],field2=['48.0'],field3=['1'],field4=['0'],field5=['0'],field6=['0']))
        self.assertEqual(self.s.get_state()['thingspeak_last_entry_id'],1234)
        self.assertGreaterEqual(app.THINGSPEAK_INTERVAL,20)

    def test_thingspeak_rejection_and_network_failure_obey_rate(self):
        self.fresh()
        for result in (b'0',b'not-an-id',OSError('offline')):
            self.s.ts_last_attempt=None
            response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
            response.read.return_value=result
            with patch.object(app,'THINGSPEAK_ENABLED',True), patch('gateway.urllib.request.urlopen',return_value=response,side_effect=result if isinstance(result,Exception) else None) as http:
                self.assertFalse(self.s._thingspeak_once())
                self.assertFalse(self.s._thingspeak_once())
                http.assert_called_once()
                self.assertEqual(self.s.thingspeak_status,'error')
        self.assertIsNone(self.s.thingspeak_last_success_at)

    def test_thingspeak_skips_stale_and_disabled(self):
        self.fresh()
        self.assertFalse(self.s._thingspeak_once())
        with patch.object(app,'THINGSPEAK_ENABLED',True):
            for key,age in (('dht_updated_at',121),('inputs_updated_at',6)):
                self.fresh();self.s.state[key]=stamp(age)
                self.assertFalse(self.s._thingspeak_once())
                self.assertEqual(self.s.thingspeak_status,'awaiting fresh sensors')

    def test_mock_forces_cloud_disabled(self):
        env = dict(os.environ, MOCK_GPIO='1', ENABLE_MQTT='1', ENABLE_THINGSPEAK='1', ALLOW_MOCK_CLOUD='0')
        result = subprocess.run([sys.executable, '-c', 'import gateway; assert not gateway.CLOUD_ENABLED; assert not gateway.THINGSPEAK_ENABLED'], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_active_low_inputs(self):
        self.s.hw.ir = SimpleNamespace(value=0, close=lambda:None)
        self.s.hw.flame = SimpleNamespace(value=1, close=lambda:None)
        with patch.object(app,'MOCK',False):
            self.assertEqual(self.s.hw.inputs(), (True,False))
            with patch.object(app,'IR_ACTIVE_LOW',False), patch.object(app,'FLAME_ACTIVE_LOW',False):
                self.assertEqual(self.s.hw.inputs(), (False,True))

    def test_broker_throttle_pauses_publishing(self):
        self.s.mqtt_connected=True
        self.message(b'ignored private broker text',topic='test-user/throttle')
        self.s.set_led('led1',True)
        self.cloud.publish.assert_not_called()
        self.assertIn('led1',self.s.dirty_leds)

    def test_slow_thingspeak_does_not_block_gpio_polling(self):
        self.fresh()
        entered, release = threading.Event(), threading.Event()
        def blocked_request(*args, **kwargs):
            entered.set(); release.wait(3); raise OSError('offline')
        with patch.object(app,'THINGSPEAK_ENABLED',True), patch('gateway.urllib.request.urlopen',side_effect=blocked_request):
            writer=threading.Thread(target=self.s._thingspeak_once)
            writer.start()
            try:
                self.assertTrue(entered.wait(1))
                self.s.worker.start()
                deadline=time.monotonic()+1
                while self.s.state['ir_detected'] and time.monotonic()<deadline: time.sleep(.01)
                self.assertFalse(self.s.state['ir_detected'])
                self.assertTrue(writer.is_alive())
            finally:
                release.set();writer.join(2)


if __name__=='__main__': unittest.main()
