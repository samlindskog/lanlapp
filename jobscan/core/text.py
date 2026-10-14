"""Text extraction: HTML to text (scripts/styles dropped, blocks kept as lines) and heading-based sections."""
import html
import re
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "li", "ul", "ol", "br", "h1", "h2", "h3", "h4", "h5",
             "h6", "tr", "td", "th", "table", "section", "article", "header",
             "footer", "dt", "dd", "dl", "blockquote", "pre", "hr"}
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

def html_to_text(raw):
    p = _TextExtractor()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001
        return html.unescape(re.sub(r"<[^>]+>", "\n", raw))
    t = "".join(p.parts)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def generic_sections(text):
    lines = (text or "").split("\n")
    heads = []
    for i, ln in enumerate(lines):
        t = ln.strip()
        if 2 <= len(t) <= 60 and not re.search(r"[.:;,!?]$", t) and len(t.split()) <= 8 \
                and (t.isupper() or t == t.title() or t[:1].isupper() and sum(w[:1].isupper() for w in t.split()) >= max(1, len(t.split()) - 2)) \
                and i + 1 < len(lines) and lines[i + 1].strip():
            heads.append((t, i))
    out = {}
    for n, (name, i) in enumerate(heads):
        j = heads[n + 1][1] if n + 1 < len(heads) else len(lines)
        body = "\n".join(lines[i + 1:j]).strip()
        if body and name not in out:
            out[name] = body
    return out
