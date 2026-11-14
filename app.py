"""Local HTTP dashboard; gateway.py owns the GPIO and cloud worker lifecycle."""
import hmac
import signal
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name('.env'))
from flask import Flask, jsonify, render_template, request
from gateway import IoTServer, MOCK, ALLOW_MOCK_CLOUD, TOKEN, WEB_HOST, WEB_PORT, LOG

web = Flask(__name__)
web.config['MAX_CONTENT_LENGTH'] = 1024
server: IoTServer | None = None


@web.after_request
def secure_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@web.get('/')
def home():
    return render_template('index.html')


@web.get('/api/state')
def state_route():
    if server is None or server.stopping.is_set():
        return jsonify({'error': 'Gateway unavailable'}), 503
    return jsonify(server.get_state())


@web.get('/healthz')
def healthz():
    healthy = server is not None and server.worker.is_alive() and not server.stopping.is_set()
    return jsonify({'status': 'ok' if healthy else 'unavailable', 'hardware_mode': not MOCK}), 200 if healthy else 503


@web.post('/api/led/<name>')
def led_route(name: str):
    if not TOKEN or not request.headers.get('X-Dashboard-Token') or not hmac.compare_digest(request.headers.get('X-Dashboard-Token', '').encode(), TOKEN.encode()):
        return jsonify({'error': 'Wrong dashboard token'}), 401
    if name not in ('led1', 'led2'):
        return jsonify({'error': 'Unknown LED'}), 404
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or type(payload.get('on')) is not bool:
        return jsonify({'error': 'Send JSON with boolean field on'}), 400
    if server is None or server.stopping.is_set():
        return jsonify({'error': 'Gateway unavailable'}), 503
    try:
        server.set_led(name, payload['on'])
    except Exception as exc:
        LOG.warning('Output command failed: %s', type(exc).__name__)
        return jsonify({'error': 'Output command failed; inspect device'}), 503
    return jsonify({'ok': True, 'name': name, 'on': server.get_state()[name]})


def main():
    global server
    if not TOKEN or len(TOKEN) < 8 or TOKEN == 'replace-with-a-unique-long-random-secret':
        raise SystemExit('Set DASHBOARD_TOKEN to a private value at least 8 characters long in .env')
    if MOCK and not ALLOW_MOCK_CLOUD:
        LOG.info('Mock GPIO forces cloud uploads OFF; set ALLOW_MOCK_CLOUD=1 ONLY for deliberate cloud sandbox testing')
    server = IoTServer()
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.start()
        LOG.info('Local dashboard: http://%s:%s', WEB_HOST, WEB_PORT)
        from waitress import serve
        serve(web, host=WEB_HOST, port=WEB_PORT, threads=4)
    finally:
        server.stop()


if __name__ == '__main__':
    main()
