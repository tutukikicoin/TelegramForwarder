"""
Feishu Bridge Service
Receives messages from TelegramForwarder (via Apprise json://) and forwards to Feishu groups.
Auto-translates English content to Chinese using Google Translate.
Supports forwarding images along with text.
"""
import time
import json
import logging
import re
import base64
import io
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

# Image extensions for detecting image attachments
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}


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


def upload_image_to_feishu(image_data):
    """Upload image to Feishu and return image_key.

    Args:
        image_data: bytes of the image file

    Returns:
        str: image_key if successful, None otherwise
    """
    token = get_tenant_token()
    if not token:
        logger.error('No valid token, cannot upload image')
        return None

    url = 'https://open.feishu.cn/open-apis/im/v1/images'

    # Build multipart/form-data manually
    boundary = f'----WebKitFormBoundary{int(time.time() * 1000)}'

    body = io.BytesIO()
    # image_type field
    body.write(f'--{boundary}\r\n'.encode())
    body.write(b'Content-Disposition: form-data; name="image_type"\r\n\r\n')
    body.write(b'message\r\n')
    # image file field
    body.write(f'--{boundary}\r\n'.encode())
    body.write(b'Content-Disposition: form-data; name="image"; filename="image.png"\r\n')
    body.write(b'Content-Type: application/octet-stream\r\n\r\n')
    body.write(image_data)
    body.write(b'\r\n')
    body.write(f'--{boundary}--\r\n'.encode())

    body_data = body.getvalue()

    req = urllib.request.Request(url, data=body_data, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': f'multipart/form-data; boundary={boundary}'
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get('code') == 0:
                image_key = result.get('data', {}).get('image_key')
                logger.info(f'Image uploaded successfully: {image_key}')
                return image_key
            else:
                logger.error(f'Failed to upload image: {result}')
                return None
    except Exception as e:
        logger.error(f'Error uploading image: {e}')
        return None


def is_image_attachment(attachment):
    """Check if an attachment is an image based on mime_type or file_name"""
    mime = attachment.get('mime_type', '')
    if mime.startswith('image/'):
        return True
    fname = attachment.get('file_name', '')
    ext = os.path.splitext(fname)[1].lower()
    return ext in IMAGE_EXTENSIONS


def parse_attachments(data):
    """Extract image data from Apprise JSON attachments.

    Apprise json:// sends: {"attachments": [{"filename": "...", "base64": "...", "mimetype": "image/..."}]}

    Returns:
        list of bytes: decoded image data for each image attachment
    """
    images = []
    attachments = data.get('attachments', [])
    if not attachments:
        return images

    for att in attachments:
        # Check mimetype or filename for image detection
        mime = att.get('mimetype', '') or att.get('mime_type', '')
        fname = att.get('filename', '') or att.get('file_name', '')
        is_image = mime.startswith('image/')
        if not is_image:
            ext = os.path.splitext(fname)[1].lower()
            is_image = ext in IMAGE_EXTENSIONS

        if not is_image:
            logger.info(f'Skipping non-image attachment: {fname} ({mime})')
            continue

        # Apprise json:// sends base64-encoded data in "base64" field
        b64_data = att.get('base64', '')
        if b64_data:
            try:
                images.append(base64.b64decode(b64_data))
                logger.info(f'Decoded image attachment: {fname} ({mime}, {len(images[-1])} bytes)')
            except Exception as e:
                logger.error(f'Failed to decode image attachment: {e}')

    return images


def build_post_content(text, image_keys):
    """Build Feishu rich text (post) content with text and images.

    Args:
        text: message text
        image_keys: list of uploaded image keys

    Returns:
        dict: Feishu post message content
    """
    content = []
    # Add text lines
    if text:
        for line in text.split('\n'):
            content.append([{"tag": "text", "text": line}])
    # Add images
    for key in image_keys:
        content.append([{"tag": "img", "image_key": key}])

    return {
        "zh_cn": {
            "content": content
        }
    }


def send_to_feishu(target, text, image_keys=None):
    """Send message to a Feishu group (via app API or webhook)"""
    if target.startswith('https://'):
        return send_via_webhook(target, text, image_keys)
    else:
        return send_via_app(target, text, image_keys)


def send_via_webhook(webhook_url, text, image_keys=None):
    """Send message via Feishu webhook with signature"""
    import hashlib
    import hmac

    sign_key = os.getenv('FEISHU_WEBHOOK_SECRET', '')
    timestamp = str(int(time.time()))

    # Use rich text if we have images, otherwise plain text
    if image_keys:
        post_content = build_post_content(text, image_keys)
        payload = {
            'msg_type': 'post',
            'content': {'post': post_content}
        }
    else:
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
                logger.info(f'Message sent via webhook (images: {len(image_keys) if image_keys else 0})')
                return True
            else:
                logger.error(f'Webhook send failed: {result}')
                return False
    except Exception as e:
        logger.error(f'Error sending via webhook: {e}')
        return False


def send_via_app(chat_id, text, image_keys=None):
    """Send message via Feishu app API"""
    token = get_tenant_token()
    if not token:
        logger.error('No valid token, cannot send message')
        return False

    url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id'

    # Use rich text if we have images, otherwise plain text
    if image_keys:
        post_content = build_post_content(text, image_keys)
        payload = {
            'receive_id': chat_id,
            'msg_type': 'post',
            'content': json.dumps({'post': post_content})
        }
    else:
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
                logger.info(f'Message sent to {chat_id} (images: {len(image_keys) if image_keys else 0})')
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
            image_keys = []
            if body:
                data = json.loads(body)
                # Apprise sends: {"version": "1.0", "title": "...", "message": "...", "type": "...", "attachments": [...]}
                text = data.get('message', '') or data.get('body', '') or str(data)
                title = data.get('title', '')
                if title and title != 'Apprise':
                    text = f"**{title}**\n{text}"

                # Parse and upload image attachments
                images = parse_attachments(data)
                if images:
                    logger.info(f'Found {len(images)} image attachment(s), uploading to Feishu...')
                    for img_data in images:
                        image_key = upload_image_to_feishu(img_data)
                        if image_key:
                            image_keys.append(image_key)
                    logger.info(f'Successfully uploaded {len(image_keys)} image(s)')
            else:
                text = ''

            # Prepend source group name
            if source_group and text:
                text = f"【{source_group}】\n{text}"

            if not text and not image_keys:
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
                if send_to_feishu(chat_id, text, image_keys if image_keys else None):
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
