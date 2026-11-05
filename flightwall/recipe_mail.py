"""Private, bounded delivery of a selected recipe, off until SMTP setup."""
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import hashlib
import json
from pathlib import Path
import re
import smtplib
import ssl

from .config import private_json

_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,32}")
_MAX_ROWS = 48
_MAX_BODY = 7000
# JSON may encode one supplementary Unicode character as 12 ASCII bytes.
_MAX_OUTBOX_BYTES = _MAX_ROWS * (12 * _MAX_BODY + 2500)


def validate_mail_settings(settings):
    if not isinstance(settings, dict) or set(settings) != {"sender", "recipient", "app_password"}:
        raise ValueError("Invalid recipe mail settings")
    sender, recipient, password = (settings[key] for key in ("sender", "recipient", "app_password"))
    if (not all(isinstance(x, str) and _ADDRESS.fullmatch(x) for x in (sender, recipient))
            or not sender.casefold().endswith("@gmail.com")
            or not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9]{16}", password)):
        raise ValueError("Use a Gmail sender, a complete recipient address, and a separate app password")
    return settings


class RecipeMail:
    def __init__(self, data_dir, lock, *, enabled=lambda: True, smtp_factory=smtplib.SMTP_SSL):
        self.directory = Path(data_dir)
        self.credentials = self.directory / "recipe-mail.json"
        self.outbox = self.directory / "recipe-outbox.json"
        self.lock = lock
        self.enabled = enabled
        self.smtp_factory = smtp_factory

    @staticmethod
    def _read(path, maximum):
        if path.is_symlink() or path.stat().st_size > maximum:
            raise ValueError("Invalid private recipe mail file")
        return json.loads(path.read_text(encoding="utf-8"))

    def _config(self):
        return validate_mail_settings(self._read(self.credentials, 4096))

    @staticmethod
    def _binding(settings):
        return hashlib.sha256((settings["sender"].lower() + "\0" + settings["recipient"].lower()).encode()).hexdigest()

    def _rows(self, now):
        try:
            rows = self._read(self.outbox, _MAX_OUTBOX_BYTES)
        except FileNotFoundError:
            return {}, False
        if not isinstance(rows, dict) or len(rows) > _MAX_ROWS:
            raise ValueError("Invalid recipe outbox")
        current = {}
        cleaned = False
        for key, row in rows.items():
            if not isinstance(row, dict):
                continue
            created = datetime.fromisoformat(row["created_at"])
            if not timedelta() <= now - created < timedelta(days=7):
                continue
            if row.get("status") == "queued" and now - created > timedelta(hours=1):
                row.pop("subject", None)
                row.pop("body", None)
                row["status"] = "expired"
                cleaned = True
            current[key] = row
        return current, cleaned or len(current) != len(rows)

    def status(self):
        """Local terminal only: never return addresses, recipes, or credentials."""
        try:
            settings = self._config()
        except FileNotFoundError:
            return {"configured": False, "status": "not_configured"}
        except (OSError, ValueError, TypeError, KeyError):
            return {"configured": False, "status": "invalid_settings"}
        try:
            with self.lock():
                rows, _ = self._rows(datetime.now(timezone.utc))
                binding = self._binding(settings)
                counts = {status: 0 for status in ("queued", "accepted", "unconfirmed", "expired")}
                for row in rows.values():
                    if row.get("binding") == binding and row.get("status") in counts:
                        counts[row["status"]] += 1
            return {"configured": True, "enabled": self.enabled(), "recent": counts}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return {"configured": True, "status": "outbox_unavailable"}

    def queue(self, option, day, generation):
        """Reserve one message per recipe/day/recipient; no I/O to mail here."""
        settings = self._config()
        binding = self._binding(settings)
        key = hashlib.sha256(f"{day}|{generation}|{option['number']}|{binding}".encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with self.lock():
            rows, cleaned = self._rows(now)
            if key in rows or len(rows) >= _MAX_ROWS:
                if cleaned:
                    private_json(self.outbox, rows)
                return
            lines = [option['title'], f"Serves {option['servings']} · about {option['minutes']} minutes"]
            if option.get('sides'):
                lines.append('Sides: ' + option['sides'])
            lines.extend(['', 'Ingredients', *('- ' + line for line in option['ingredients']), '', 'Directions',
                          *(f'{i}. {line}' for i, line in enumerate(option['steps'], 1))])
            body = '\n'.join(lines)
            if len(body) > _MAX_BODY:
                raise ValueError("Recipe message exceeds its limit")
            rows[key] = {"binding": binding, "created_at": now.isoformat(), "status": "queued",
                         "subject": "Flight Wall recipe: " + option['title'], "body": body}
            private_json(self.outbox, rows)

    def poll(self):
        """Reserve before SMTP; never auto-retry an uncertain submission."""
        settings = self._config()
        binding = self._binding(settings)
        now = datetime.now(timezone.utc)
        with self.lock():
            rows, cleaned = self._rows(now)
            candidate = next(((key, row) for key, row in rows.items() if row.get('status') == 'queued'
                              and row.get('binding') == binding
                              and 0 <= (now - datetime.fromisoformat(row['created_at'])).total_seconds() <= 3600), None)
            if candidate is None:
                if cleaned:
                    private_json(self.outbox, rows)
                return
            key, row = candidate
            subject, body = row.pop('subject'), row.pop('body')
            row['status'] = 'unconfirmed'
            private_json(self.outbox, rows)
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = settings['sender'], settings['recipient'], subject
        message['Message-ID'] = f'<{key}@flightwall.local>'
        message.set_content(body)
        if not self.enabled() or self._config() != settings:
            return
        with self.smtp_factory('smtp.gmail.com', 465, timeout=8, context=ssl.create_default_context()) as smtp:
            smtp.login(settings['sender'], settings['app_password'])
            if not self.enabled() or self._config() != settings:
                return
            if smtp.send_message(message):
                return
        with self.lock():
            rows, _ = self._rows(datetime.now(timezone.utc))
            if key in rows:
                rows[key]['status'] = 'accepted'
                private_json(self.outbox, rows)
