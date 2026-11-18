"""WSGI endpoint for the exterior offer form."""
import base64
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

COPY_TO = "m7823445@gmail.com"
EMAIL = re.compile(r"[A-Za-z0-9_+%'-]+(?:\.[A-Za-z0-9_+%'-]+)*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHOTO = re.compile(r"assets/exterior/(?:[A-Za-z0-9_()-]+|design/[1-9][0-9]{0,5})\.webp")
ROOFS = dict(gray="Серый", graphite="Графит", brown="Шоколад", terra="Терракота")
GUTTERS = dict(gray="Серый", white="Белый", brown="Коричневый")
FACADES = dict(gray="Серый", sand="Песочный", white="Молочный")

limits = {}
limit_lock = threading.Lock()

def reserve(ip, recipient):
    now = time.monotonic()
    with limit_lock:
        for key in list(limits):
            if limits[key] <= now:
                del limits[key]
        keys = (("ip", ip), ("email", recipient.lower()))
        if len(limits) >= 4096 or any(key in limits for key in keys):
            return False
        for key, delay in zip(keys, (30, 300)):
            limits[key] = now + delay
        return True

def selection_text(variant, image):
    if variant.startswith("design/"):
        if image == f"assets/exterior/{variant}.webp":
            return "Дизайн-вариант № " + variant.split("/")[1]
        raise ValueError("invalid design")
    roof, gutter, facade = variant.split("/")
    if "/design/" in image:
        raise ValueError("invalid finish")
    return f"Кровля — {ROOFS[roof]}\nВодосток — {GUTTERS[gutter]}\nФасад — {FACADES[facade]}"

def send_offer(email, phone, selection, image):
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return "RESEND_API_KEY not found in environment"
    
    sender = "support@lockrion.com"
    html_body = (
        "<h2>Новая заявка с сайта ХАУЗ.БЕЛ</h2>"
        f"<p><b>Телефон:</b> {phone}</p>"
        f"<p><b>Почта:</b> {email}</p>"
        f"<p><b>Выбор:</b><br>{selection.replace(chr(10), '<br>')}</p>"
    )
    
    # Кодируем фото в base64
    encoded_photo = base64.b64encode(image).decode()
    
    payload = json.dumps({
        "from": sender,
        "to": ["m7823445@gmail.com", email],
        "subject": "ХАУЗ.БЕЛ — новая заявка",
        "html": html_body,
        "attachments": [
            {
                "filename": "preview.webp",
                "content": encoded_photo
            }
        ]
    }).encode()
    
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "house-offer-app/1.0",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return None  # успех
    except urllib.error.HTTPError as e:
        return f"Resend error {e.code}: {e.read().decode(errors='replace')}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"

def application(environ, start_response):
    def reply(status, **body):
        payload = json.dumps(body).encode()
        start_response(status, [
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(payload))),
        ])
        return [payload]

    if environ.get("REQUEST_METHOD") == "OPTIONS":
        start_response("204 No Content", [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Access-Control-Max-Age", "86400"),
            ("Content-Length", "0"),
        ])
        return []

    if environ.get("PATH_INFO") != "/api/exterior-offer":
        return reply("404 Not Found", error="not_found")
    if environ.get("REQUEST_METHOD") != "POST":
        return reply("405 Method Not Allowed", error="method")
    if environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
        return reply("415 Unsupported Media Type", error="format")

    try:
        length = int(environ.get("CONTENT_LENGTH", 0))
        if not 0 < length <= 2048:
            raise ValueError("invalid length")
        data = json.loads(environ["wsgi.input"].read(length))
        email, phone, variant, image = (data[key].strip() for key in ("email", "phone", "variant", "image"))
        if len(email) > 254 or not EMAIL.fullmatch(email):
            raise ValueError("invalid email")
        if not re.fullmatch(r"[+0-9() .-]{7,32}", phone) or not 7 <= sum(c.isdigit() for c in phone) <= 15:
            raise ValueError("invalid phone")
        if not PHOTO.fullmatch(image):
            raise ValueError("invalid photo")
        selection = selection_text(variant, image)
    except Exception as e:
        print(f"VALIDATION_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return reply("400 Bad Request", error="invalid_request")

    root = Path(os.environ.get("OFFER_ASSET_ROOT", Path(__file__).parent)).resolve()
    photo = (root / image).resolve()
    if not photo.is_relative_to(root / "assets/exterior") or not photo.is_file():
        return reply("400 Bad Request", error="missing_photo")
    if not reserve(environ.get("REMOTE_ADDR", ""), email):
        return reply("429 Too Many Requests", error="rate_limit")

    try:
        with photo.open("rb") as stream:
            attachment = stream.read(10 * 1024 * 1024 + 1)
        if not (attachment[:4] == b"RIFF" and attachment[8:12] == b"WEBP") or len(attachment) > 10 * 1024 * 1024:
            return reply("400 Bad Request", error="invalid_photo")
        
        err = send_offer(email, phone, selection, attachment)
        if err:
            return reply("502 Bad Gateway", error=err)
            
    except Exception as e:
        print(f"SEND_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return reply("502 Bad Gateway", error=f"Exception: {e}")

    return reply("200 OK", ok=True)