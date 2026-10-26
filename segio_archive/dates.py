"""Compare archived dates as instants, retaining original display strings."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))


def text_timestamp(value):
    value=str(value or '').strip()
    if not value:
        return None
    try:
        parsed=parsedate_to_datetime(value)
    except (ValueError, TypeError, IndexError, OverflowError):
        try:
            parsed=datetime.fromisoformat(value.replace(' KST','+09:00'))
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed=parsed.replace(tzinfo=KST)
    try:
        return parsed.timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def approval_timestamp(value):
    if value is None or not str(value).strip():
        return None
    try:
        stamp=int(value)
        return datetime.fromtimestamp(stamp,timezone.utc).timestamp() if stamp>0 else None
    except (ValueError, TypeError, OSError, OverflowError):
        return text_timestamp(value)


class DateOrder:
    def __init__(self, db, approvals):
        self.approvals=approvals
        self.mail={}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_records'").fetchone():
            self.mail={identity:text_timestamp(date) for identity,date in db.execute('SELECT id,date FROM mail_records')}
        db.create_function('archive_date',3,self.key,deterministic=True)

    def key(self,kind,identity,path):
        return self.approvals.info(path).get('date_sort') if kind=='approval' else self.mail.get(identity)
