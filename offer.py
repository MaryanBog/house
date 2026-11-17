"""WSGI endpoint for the exterior offer form."""
import json
import os
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
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
    sender = os.environ["OFFER_SMTP_FROM"]
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    message["Subject"] = "ХАУЗ.БЕЛ — ваш вариант отделки дома"
    message.set_content(
        "Здравствуйте!\n\nСпасибо за заявку на предложение. Фото выбранного дома во вложении. "
        "Мы свяжемся с вами, чтобы уточнить детали и подготовить предложение.\n\n"
        f"{selection}\n\nТелефон из заявки: {phone}\n\nХАУЗ.БЕЛ"
    )
    message.add_attachment(image, maintype="image", subtype="webp", filename="exterior.webp")
    with smtplib.SMTP_SSL(
        os.environ["OFFER_SMTP_HOST"], int(os.environ.get("OFFER_SMTP_PORT", "465")),
        timeout=15, context=ssl.create_default_context(),
    ) as smtp:
        smtp.login(os.environ["OFFER_SMTP_USER"], os.environ["OFFER_SMTP_PASSWORD"])
        return smtp.send_message(message, to_addrs=list(dict.fromkeys((email, COPY_TO))))

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
    except (ValueError, KeyError, TypeError, AttributeError):
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
        refused = send_offer(email, phone, selection, attachment)
        if refused:
            return reply("502 Bad Gateway", error="partial_delivery")
    except (OSError, smtplib.SMTPException, KeyError, ValueError):
        return reply("502 Bad Gateway", error="delivery_failed")

    return reply("200 OK", ok=True)
