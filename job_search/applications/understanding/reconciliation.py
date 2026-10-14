"""Conservative interpretation rules shared by projection and backlog reconciliation."""
import re


def non_actionable_contact(content):
    if content.get("kind") != "contact":
        return False
    value = content.get("value")
    kind = value.get("kind", "") if isinstance(value, dict) else ""
    quotes = " ".join(e.get("quote", "") for e in content.get("evidence", []))
    if kind in {"marketing", "acknowledgment", "review", "recruiting_team"}:
        return True
    return bool(re.fullmatch(r"(?:The )?(?:Hiring |Recruiting )?Team(?: @| at) .+", quotes.strip(), re.I) or
                re.search(r"(?:will review your application|will be in touch if|get back to you if there are next steps)", quotes, re.I))


def verification(content):
    value = content.get("value")
    return content.get("kind") == "submission" and isinstance(value, dict) and value.get("status") == "pending_verification"


def receipt_fact(content):
    if content.get("kind") != "submission":
        return False
    value = content.get("value")
    status = value.get("status") if isinstance(value, dict) else value
    quotes = " ".join(e.get("quote", "") for e in content.get("evidence", []))
    return status in {"received", "confirmed"} or status in {"submitted", "attempted"} and bool(
        re.search(r"(?:thanks|thank you) for applying|(?:we.ve |we have )?received your application|application (?:has been |was )?received", quotes, re.I))


def non_actionable_request(content):
    """A completion acknowledgment is not an instruction to complete it again."""
    if content.get("kind") not in {"other", "follow_up"} or "outcome" not in content:
        return False
    quotes = " ".join(e.get("quote", "") for e in content.get("evidence", []))
    return bool(re.fullmatch(r"(?:Thank you|Thanks) for (?:completing|submitting) (?:your |the )?application[.!]?", quotes.strip(), re.I) or
        re.search(r"(?:copy|paste|enter|use).{0,80}(?:security|verification|one.time) code|(?:copy|paste|enter|use).{0,40}code.{0,60}(?:security code field|verify|verification)", quotes, re.I))


def only_nonactionable(findings):
    business = [f for f in findings if f['category'] in {'facts', 'requests'}]
    return bool(business) and all(
        non_actionable_contact(f['content']) or verification(f['content']) or non_actionable_request(f['content'])
        or f['category'] == 'requests' and (f['content'].get('requirement') != 'required' or f['content'].get('responsible_party') != 'applicant')
        for f in business)
