"""Mask identifiers in message text before it leaves the machine.

Masks links, @-handles, emails and phone-like numbers. Names written in
plain text are NOT masked automatically: see SKILL.md, section "Privacy".
"""
import re

URL = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
HANDLE = re.compile(r"@\w+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"\+?\d[\d\s()-]{8,}\d")


def anonymize(text: str) -> str:
    text = URL.sub("[ссылка]", text)
    text = EMAIL.sub("[email]", text)
    text = HANDLE.sub("[ник]", text)
    text = PHONE.sub("[телефон]", text)
    return text
