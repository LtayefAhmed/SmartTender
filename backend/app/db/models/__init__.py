"""ORM models.

Importing this package registers every mapper on ``Base.metadata``, which is
what Alembic autogenerate and ``create_all`` need. Import it — not the
individual modules — anywhere metadata completeness matters.
"""

from app.db.models.cv import CV
from app.db.models.cv_profile import CVProfile
from app.db.models.job import ConnectorRun, ScrapingJob
from app.db.models.log import ExecutionLog
from app.db.models.notification import Notification, UserPreference
from app.db.models.schedule import Schedule, ScheduleChangeSentinel
from app.db.models.source import Source
from app.db.models.submission import Submission
from app.db.models.tender import DuplicateRecord, Tender, TenderDocument, TenderScore

__all__ = [
    "CV",
    "CVProfile",
    "ConnectorRun",
    "DuplicateRecord",
    "ExecutionLog",
    "Notification",
    "Schedule",
    "ScheduleChangeSentinel",
    "ScrapingJob",
    "Source",
    "Submission",
    "Tender",
    "TenderDocument",
    "TenderScore",
    "UserPreference",
]
