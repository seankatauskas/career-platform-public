"""One transactional lifecycle migration, composed by domain ownership."""
from .core_schema import SCHEMA as CORE
from .mail_schema import SCHEMA as MAIL
from .interview_schema import SCHEMA as INTERVIEW
SCHEMA = CORE + MAIL + INTERVIEW + "\nPRAGMA user_version = 15;\n"
