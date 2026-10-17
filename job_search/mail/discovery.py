"""Conservative, provider-independent identity hints for human mail review."""
import re
import unicodedata
from difflib import SequenceMatcher


def identity_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def related_employer(left, right):
    def key(value):
        value = re.sub(r'\b(?:incorporated|inc|llc|ltd|limited|corporation|corp)\.?$', '', value.strip(), flags=re.I)
        return identity_key(value)
    a, b = key(left), key(right)
    def initials(value):
        words = re.findall(r'[^\W\d_]+', value, flags=re.UNICODE)
        return ''.join(word[0].casefold() for word in words) if len(words) >= 2 else ''
    return bool(a and b) and (a == b or (len(a) >= 2 and a == initials(right)) or
        (len(b) >= 2 and b == initials(left)) or (min(len(a), len(b)) >= 4 and
        (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= .8)))


def related_role(left, right):
    a, b = identity_key(left), identity_key(right)
    words = lambda value: set(re.findall(r'\w+', value.casefold())) - {'senior', 'junior', 'sr', 'jr', 'the', 'of', 'and', 'i', 'ii', 'iii'}
    return bool(a and b) and (a == b or a in b or b in a or
        bool(words(left) & words(right)) or SequenceMatcher(None, a, b).ratio() >= .75)


def explicit_identity(subject, body):
    """Hints require a unique explicit employer and role, never an ATS sender."""
    from .sanitizer import sanitize_mail
    current = sanitize_mail(subject, body, max_chars=max(1024, len(subject) + len(body) + 512), drop_quoted_history=True)
    text = current.subject + '\n' + current.body
    employers = re.findall(r'(?im)^\s*(?:company|employer)\s*:\s*([^\n]{2,160})$', text)
    titles = re.findall(r'(?im)^\s*(?:job title|position|role)\s*:\s*([^\n]{2,160})$', text)
    for match in re.finditer(
        r'(?i)\b(?:applying|application|interest|interview) for (?:the )?'
        r'([^\n.!?]{2,160}?) (?:role |position )?at ([^\n!?]{2,160}?)(?=\.\s|[.!?\n]|$)', text):
        titles.append(re.sub(r'\s+(?:role|position)$', '', match[1], flags=re.I))
        employers.append(match[2])
    # A separate explicit company sentence can accompany a labeled job title.
    employers.extend(re.findall(r'(?i)\b(?:thank you|thanks) for applying to ([^\n!?]{2,160}?)(?=\s+for\b|\.\s|[.!?\n]|$)', text))
    def unique(values):
        values = {identity_key(v): v.strip(' .,:;\"') for v in values if identity_key(v)}
        return next(iter(values.values())) if len(values) == 1 else ''
    employer, title = unique(employers), unique(titles)
    return {'employer': employer, 'title': title}
