"""
Feishu Bridge Service
Receives messages from TelegramForwarder (via Apprise json://) and forwards to Feishu groups.
Auto-translates English content to Chinese using Google Translate.
"""
import time
import json
import logging
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.parse
import urllib.error
import os
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

APP_ID = os.getenv('FEISHU_APP_ID', 'cli_a95902db34b8dbc9')
APP_SECRET = os.getenv('FEISHU_APP_SECRET', '0u7wB9AimKclxwmk7bY25g5BCLFwUFyc')
DEFAULT_CHAT_IDS = os.getenv('FEISHU_CHAT_IDS', '').split(',')

# Route mapping: source group name -> feishu chat_id
# Priority: JSON file > env var
ROUTES_FILE = os.getenv('FEISHU_ROUTES_FILE', '/app/db/feishu_routes.json')
ROUTES = {}

def load_routes():
    """Load routes from JSON file, fallback to env var"""
    global ROUTES
    # Try JSON file first
    if os.path.exists(ROUTES_FILE):
        try:
            with open(ROUTES_FILE, 'r', encoding='utf-8') as f:
                ROUTES = json.load(f)
            logger.info(f'Loaded {len(ROUTES)} routes from {ROUTES_FILE}')
            return
        except Exception as e:
            logger.error(f'Failed to load routes file: {e}')
    # Fallback to env var
    routes_str = os.getenv('FEISHU_ROUTES', '')
    if routes_str:
        for item in routes_str.split('|'):
            if '=' in item:
                name, chat_id = item.split('=', 1)
                ROUTES[name.strip()] = chat_id.strip()

load_routes()

# Token cache
_token_cache = {'token': None, 'expires_at': 0}
_token_lock = threading.Lock()


def is_mostly_english(text):
    """Check if text is mostly English (>50% ASCII letters)"""
    if not text:
        return False
    # Remove URLs, numbers, symbols
    clean = re.sub(r'https?://\S+', '', text)
    clean = re.sub(r'[0-9\s\W]+', '', clean)
    if not clean:
        return False
    ascii_count = sum(1 for c in clean if ord(c) < 128)
    return ascii_count / len(clean) > 0.5


def google_translate(text, target='zh-CN'):
    """Translate text using Google Translate free API"""
    try:
        # Split long text into chunks (Google has a limit)
        max_len = 4500
        if len(text) <= max_len:
            chunks = [text]
        else:
            # Split by paragraphs
            parts = text.split('\n')
            chunks = []
            current = ''
            for part in parts:
                if len(current) + len(part) + 1 > max_len:
                    if current:
                        chunks.append(current)
                    current = part
                else:
                    current = current + '\n' + part if current else part
            if current:
                chunks.append(current)

        translated_parts = []
        for chunk in chunks:
            encoded = urllib.parse.quote(chunk)
            url = f'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target}&dt=t&q={encoded}'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                translated = ''.join(item[0] for item in result[0] if item[0])
                translated_parts.append(translated)

        return '\n'.join(translated_parts)
    except Exception as e:
        logger.error(f'Translation failed: {e}')
        return None


def get_tenant_token():
    """Get or refresh tenant access token"""
    with _token_lock:
        now = time.time()
        if _token_cache['token'] and now < _token_cache['expires_at'] - 60:
            return _token_cache['token']

        url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
        data = json.dumps({'app_id': APP_ID, 'app_secret': APP_SECRET}).encode()
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if result.get('code') == 0:
                    _token_cache['token'] = result['tenant_access_token']
                    _token_cache['expires_at'] = now + result.get('expire', 7200)
                    logger.info('Feishu token refreshed successfully')
                    return _token_cache['token']
                else:
                    logger.error(f'Failed to get token: {result}')
                    return None
        except Exception as e:
            logger.error(f'Error getting token: {e}')
            return None


def send_to_feishu(target, text):
    """Send message to a Feishu group (via app API or webhook)"""
    if target.startswith('https://'):
        return send_via_webhook(target, text)
    else:
        return send_via_app(target, text)


def send_via_webhook(webhook_url, text):
    """Send message via Feishu webhook with signature"""
    import hashlib
    import hmac
    import base64

    sign_key = os.getenv('FEISHU_WEBHOOK_SECRET', '')
    timestamp = str(int(time.time()))

    payload = {'msg_type': 'text', 'content': {'text': text}}

    if sign_key:
        # Feishu webhook signature: HMAC-SHA256(secret, timestamp + "\n" + secret)
        string_to_sign = f'{timestamp}\n{sign_key}'
        hmac_code = hmac.new(string_to_sign.encode('utf-8'), b'', hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode('utf-8')
        payload['timestamp'] = timestamp
        payload['sign'] = sign

    data = json.dumps(payload).encode()
    req = urllib.request.Request(webhook_url, data=data, headers={'Content-Type': 'application/json'})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get('StatusCode') == 0 or result.get('code') == 0:
                logger.info(f'Message sent via webhook')
                return True
            else:
                logger.error(f'Webhook send failed: {result}')
                return False
    except Exception as e:
        logger.error(f'Error sending via webhook: {e}')
        return False


def send_via_app(chat_id, text):
    """Send message via Feishu app API"""
    token = get_tenant_token()
    if not token:
        logger.error('No valid token, cannot send message')
        return False

    url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id'
    payload = {
        'receive_id': chat_id,
        'msg_type': 'text',
        'content': json.dumps({'text': text})
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get('code') == 0:
                logger.info(f'Message sent to {chat_id}')
                return True
            else:
                logger.error(f'Failed to send message: {result}')
                return False
    except Exception as e:
        logger.error(f'Error sending message: {e}')
        return False


class BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length else ''

        logger.info(f'Received POST {self.path}, body length: {len(body)}')

        try:
            # Extract source group name from URL path
            source_group = urllib.parse.unquote(self.path.strip('/')) if self.path and self.path != '/' else ''

            # Parse the message from Apprise json:// format
            if body:
                data = json.loads(body)
                # Apprise sends: {"title": "...", "body": "...", "type": "..."}
                text = data.get('body', '') or data.get('message', '') or str(data)
                title = data.get('title', '')
                if title and title != 'Apprise':
                    text = f"**{title}**\n{text}"
            else:
                text = ''

            # Prepend source group name
            if source_group and text:
                text = f"【{source_group}】\n{text}"

            if not text:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "empty"}')
                return

            # Auto-translate English to Chinese
            if is_mostly_english(text):
                logger.info('Detected English content, translating...')
                translated = google_translate(text)
                if translated:
                    text = f"{text}\n\n--- 翻译 ---\n{translated}"
                    logger.info('Translation appended')

            # Reload routes on each request for dynamic updates
            load_routes()

            # Determine target Feishu chat(s)
            target_chats = []
            if source_group and source_group in ROUTES:
                target_chats = [ROUTES[source_group]]
            else:
                target_chats = [c.strip() for c in DEFAULT_CHAT_IDS if c.strip()]

            success = False
            for chat_id in target_chats:
                if send_to_feishu(chat_id, text):
                    success = True

            self.send_response(200 if success else 500)
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok' if success else 'failed'}).encode())

        except Exception as e:
            logger.error(f'Error processing request: {e}')
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def do_GET(self):
        """Health check"""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "running"}')

    def log_message(self, format, *args):
        logger.info(f'{self.address_string()} - {format % args}')


if __name__ == '__main__':
    port = int(os.getenv('BRIDGE_PORT', '8001'))
    server = HTTPServer(('0.0.0.0', port), BridgeHandler)
    logger.info(f'Feishu Bridge starting on port {port}')
    logger.info(f'Default chats: {DEFAULT_CHAT_IDS}')
    logger.info(f'Routes: {ROUTES}')

    # Verify token on startup
    token = get_tenant_token()
    if token:
        logger.info('Initial token acquired successfully')
    else:
        logger.warning('Failed to get initial token, will retry on first message')

    server.serve_forever()
