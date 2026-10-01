"""Render untrusted saved posting text with a small, inert formatting vocabulary."""

from html import escape, unescape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit


_TAGS = frozenset('p div span br hr h1 h2 h3 h4 h5 h6 ul ol li strong b em i u s blockquote pre code a table thead tbody tr th td dl dt dd'.split())
_VOID = frozenset({'br', 'hr'})
_HIDDEN = frozenset('script style iframe object svg math template noscript'.split())
_MARKUP = re.compile(r'</?[a-z][a-z0-9]*(?:\s[^<>]*|\s*)/?>', re.I)


class _DescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.hidden = []
        self.has_text = False

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag in _HIDDEN:
                self.hidden.append(tag)
            return
        if tag in _HIDDEN:
            self.hidden.append(tag)
            return
        if tag not in _TAGS:
            return
        # Bound nesting so malformed vendor HTML cannot create an enormous DOM depth.
        if len(self.stack) >= 64:
            return
        attributes = ''
        if tag == 'a':
            href = dict(attrs).get('href') or ''
            try:
                parsed = urlsplit(href)
                safe = parsed.scheme.lower() in {'https', 'http'} and bool(parsed.netloc)
            except ValueError:
                safe = False
            if safe:
                attributes = f' href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
        self.parts.append(f'<{tag}{attributes}>')
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f'</{current}>')
                if current == tag:
                    break

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(escape(data))
            self.has_text = self.has_text or bool(data.strip())


def render_description(value: str) -> str:
    """Keep readable formatting, never scripts, media, styles, or embedded forms.

    Some ATSs encode the whole HTML document; decode only when that reveals
    markup. Plain text keeps its line breaks and literal comparison operators.
    """
    text = str(value or '').strip()
    for _ in range(2):
        if _MARKUP.search(text):
            break
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    if not _MARKUP.search(text):
        return f'<p>{escape(text).replace(chr(10), "<br>")}</p>' if text else ''
    parser = _DescriptionParser()
    parser.feed(text)
    parser.close()
    if not parser.has_text:
        return ''
    return ''.join(parser.parts + [f'</{tag}>' for tag in reversed(parser.stack)])
