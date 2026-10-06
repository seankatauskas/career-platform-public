"""Conservative reply-task evidence, independent of application stage changes.

The mail classifier determines the application event. A reply task additionally
needs a request addressed to the applicant: punctuation, receipt boilerplate and
an invitation to ask questions are not evidence that the applicant owes a reply.
Unrecognized wording stays available in the conversation for review.
"""
from __future__ import annotations

import re

from .slots import authored_text


REPLY_POLICY_VERSION = "explicit-reply-request-v2"

_URL = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
_OPTIONAL_CONTACT = re.compile(
    r"\b(?:if|should|whenever)\b[^.!?\n]*\b"
    r"(?:questions?|concerns?|queries|assistance|help|clarification)\b"
    r"|\b(?:feel free|do not hesitate|don't hesitate|don’t hesitate)\b"
    r"|\b(?:any|have) questions\s*\?",
    re.IGNORECASE,
)
_NO_REPLY = re.compile(
    r"\b(?:do not|don't|don’t|please don't|please don’t|no need to)\s+"
    r"(?:reply|respond|send|share|provide|confirm)\b"
    r"|\b(?:no (?:reply|response)(?: is)?|(?:reply|response) is not)\s+"
    r"(?:needed|required|necessary|expected)\b",
    re.IGNORECASE,
)
_REQUEST = re.compile(
    r"\b(?:please|kindly)\s+(?:reply|respond|confirm|send|share|provide|tell)\b"
    r"|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"(?:reply|respond|confirm|send|share|provide|tell|let|attend|join|meet|chat)\b"
    r"|\blet\s+(?:me|us)\s+know\b"
    r"|\b(?:are you|would you be)\s+(?:available|free|interested|able|willing)\b"
    r"|\b(?:what|when|which)\s+(?:is|are|would be)\s+your\s+"
    r"(?:availability|available times|preferred times|salary expectations|notice period|earliest start date)\b"
    r"|\bwhen\s+(?:can|could|would)\s+you\s+(?:start|meet|talk|chat|speak|interview)\b",
    re.IGNORECASE,
)


def reply_request_evidence(excerpt: str) -> str | None:
    """Return bounded request wording, or abstain; never infer an interview.

    Strip transport links before splitting sentences. In particular the query
    marker in an ATS report-abuse URL must not look like a recruiter question.
    Keep paragraph context so an optional support invitation cannot lose its
    condition when it contains a question mark or a line-wrapped sentence.
    """
    text = _URL.sub("", authored_text(excerpt))
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = " ".join(paragraph.split())
        if not paragraph or _NO_REPLY.search(paragraph):
            continue
        optional_contact = False
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            # "Any questions? Please reply." is one optional support invitation.
            optional_contact = optional_contact or bool(_OPTIONAL_CONTACT.search(sentence))
            if optional_contact:
                continue
            if _REQUEST.search(sentence):
                return sentence[:600]
    return None
