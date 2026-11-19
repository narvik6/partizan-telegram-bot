"""Two independently sampled nouns; dictionary is shipped with the service."""
import base64
import secrets
import unicodedata
from pathlib import Path

WORDS = tuple(Path(__file__).with_name('words_ru.txt').read_text(encoding='utf-8').split())
assert len(WORDS) == len(set(WORDS)) and len(WORDS) >= 256


def generate():
    return secrets.choice(WORDS) + ' ' + secrets.choice(WORDS)


def normalize(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def authorization_key(value):
    if value.startswith('phrase.'):
        data = value[7:]
        decoded = base64.b64decode(data + '=' * (-len(data) % 4), altchars=b'-_', validate=True)
        phrase = normalize(decoded.decode('utf-8'))
        if len(phrase.split()) != 2 or any(w not in WORDS for w in phrase.split()):
            raise ValueError('Invalid phrase')
        return phrase
    return value  # Existing long connection codes remain usable.
