"""Flask integration tests; requires the real project dependencies."""
import unittest
from unittest.mock import Mock
try:
    import flask
    import dotenv
except ImportError as exc:
    raise unittest.SkipTest('Install requirements.txt to run Flask integration tests') from exc
# Configure dummy credentials and disabled integrations before importing the web app.
import test_mock
import app

class APITests(unittest.TestCase):
    def setUp(self):
        self.s = app.IoTServer()
        app.server = self.s
        self.client = app.web.test_client()
        self.auth = {'X-Dashboard-Token': 'test-demo-secret'}
        self.addCleanup(self.s.stop)

    def test_dashboard_and_state(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertIn(b'SIMULATION MODE', self.client.get('/').data)
        data = self.client.get('/api/state').json
        self.assertTrue(data['mock_mode'])
        self.assertFalse(data['cloud_enabled'])
        self.assertFalse(data['thingspeak_enabled'])
        self.assertEqual(data['pins_bcm'], dict(dht=22, ir=17, flame=27, led1=5, led2=6, led2_mirror=None))
        self.assertNotIn('test-demo-secret', str(data))
        self.assertNotIn('test-aio-key', str(data))
        self.assertEqual(self.client.get('/api/state').headers['Cache-Control'], 'no-store')

    def test_auth_and_independent_boolean_outputs(self):
        for name in ('led1', 'led2'):
            self.assertEqual(self.client.post('/api/led/'+name,json={'on':True}).status_code,401)
            for enabled in (True, False):
                r=self.client.post('/api/led/'+name,json={'on':enabled},headers=self.auth)
                self.assertEqual(r.status_code,200)
                self.assertEqual(self.s.hw.leds[name].is_lit,enabled)
                self.assertEqual(r.json['on'],enabled)
        self.s.set_led('led1',True)
        self.assertFalse(self.s.get_state()['led2'])

    def test_invalid_requests_and_get_never_mutate(self):
        for payload in ({'on':'yes'},{'on':1},{'on':None},{},[],True):
            self.assertEqual(self.client.post('/api/led/led1',json=payload,headers=self.auth).status_code,400)
        self.assertEqual(self.client.post('/api/led/led1',data='{',content_type='application/json',headers=self.auth).status_code,400)
        self.assertEqual(self.client.get('/api/led/led1?on=true').status_code,405)
        self.assertEqual(self.client.post('/api/led/relay',json={'on':True},headers=self.auth).status_code,404)
        self.assertEqual(self.client.post('/api/led/led1',json={'on':True},headers={'X-Dashboard-Token':'wrong'}).status_code,401)
        self.assertFalse(self.s.state['led1'])

    def test_output_failure_is_not_reported_success(self):
        self.s.hw.leds['led1'].on=Mock(side_effect=OSError('GPIO unavailable'))
        r=self.client.post('/api/led/led1',json={'on':True},headers=self.auth)
        self.assertEqual(r.status_code,503)
        self.assertFalse(self.s.state['led1'])

    def test_health_tracks_worker_and_shutdown(self):
        self.assertEqual(self.client.get('/healthz').status_code,503)
        self.s.start()
        self.assertEqual(self.client.get('/healthz').status_code,200)
        self.s.stop()
        self.assertEqual(self.client.get('/healthz').status_code,503)
        self.assertEqual(self.client.get('/api/state').status_code,503)
