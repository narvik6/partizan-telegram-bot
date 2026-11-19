"""Authenticate the original SSO sender and the recipient before granting access."""
import hashlib
import email.utils
from datetime import datetime, timezone
import dkim
from mail_service import address


def original_identity(raw, message, expected_sender, dnsfunc=None):
    """No trusted result is inferred from arbitrary Authentication-Results text."""
    if any(len(message.get_all(name, [])) != 1 for name in ('From', 'To', 'Date', 'Subject')):
        return None
    sender = address(email.utils.parseaddr(str(message['From']))[1])
    if not sender or sender != address(expected_sender):
        return None
    recipients = {address(value) for _, value in email.utils.getaddresses(message.get_all('To', [])) if address(value)}
    if len(recipients) != 1:
        return None
    try:
        sent = email.utils.parsedate_to_datetime(str(message['Date']))
        if sent.tzinfo is None or not -60 <= (datetime.now(timezone.utc) - sent).total_seconds() <= 900:
            return None
    except (ValueError, TypeError):
        return None
    for index, value in enumerate(message.get_all('DKIM-Signature', [])):
        try:
            tags = dkim.util.parse_tag_value(str(value).encode('ascii'))
            signed = {part.strip().lower() for part in tags.get(b'h', b'').split(b':')}
            domain = tags.get(b'd', b'').decode('ascii').casefold()
            sender_domain = sender.rsplit('@', 1)[1]
            if not domain or not (sender_domain == domain or sender_domain.endswith('.' + domain)):
                continue
            if not {b'from', b'to', b'date', b'subject'} <= signed or b'l' in tags:
                continue
            options = {'dnsfunc': dnsfunc} if dnsfunc else {}
            if dkim.DKIM(raw, timeout=5).verify(idx=index, **options):
                signature = b''.join(tags[b'b'].split())
                return {'recipient': next(iter(recipients)), 'sent_at': sent.isoformat(),
                        'id': 'dkim:' + hashlib.sha256(signature).hexdigest()}
        except Exception:
            # Missing DNS/key, invalid/unsupported signature => no automatic grant.
            continue
    return None
