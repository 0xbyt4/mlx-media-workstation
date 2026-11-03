"""HTML to readable text with the standard library's parser (no trafilatura).

Drops scripts, styles, forms, navigation, headers, footers and asides. When the page has a <main> or <article> with
real text, only that part is kept. Headings become "# " lines, list items "- " lines, and the absolute http(s)
links of the kept part are listed at the end (up to MAX_LINKS), so the model can fetch a linked page next.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

SKIP = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "form",
    "button",
    "select",
    "nav",
    "footer",
    "header",
    "aside",
    "head",
}
BLOCK = {
    "p",
    "div",
    "section",
    "br",
    "tr",
    "table",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "pre",
    "blockquote",
    "figure",
    "figcaption",
    "hr",
    "main",
    "article",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
VOID = {
    "br",
    "hr",
    "img",
    "input",
    "meta",
    "link",
    "area",
    "base",
    "col",
    "source",
    "wbr",
}
MAIN = {"main", "article"}
MIN_MAIN = 200  # characters a <main>/<article> needs to replace the whole page
MAX_LINKS = 30
KEEP = "\ue000"


class _Extract(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.in_title = False
        self.skip = 0  # depth inside skipped elements
        self.main = 0  # depth inside main/article
        self.pre = 0
        self.all: list[str] = []
        self.main_parts: list[str] = []
        self.links: list[tuple[str, bool]] = []  # (url, inside main)

    def _emit(self, text: str) -> None:
        self.all.append(text)
        if self.main:
            self.main_parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in SKIP:
            if tag not in VOID:
                self.skip += 1
            return
        if self.skip:
            return
        if tag in MAIN:
            self.main += 1
        if tag == "pre":
            self.pre += 1
        if tag in BLOCK:
            self._emit("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._emit("- ")
        elif tag in ("td", "th"):
            self._emit(" | ")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                url = urljoin(self.base, href.strip())
                if urlsplit(url).scheme in ("http", "https"):
                    self.links.append((url.split("#", 1)[0], self.main > 0))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in BLOCK:
            self._emit("\n")
        if tag in MAIN:
            self.main = max(0, self.main - 1)
        if tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        # spaces in <pre> survive the line strip in _tidy as a private-use character
        self._emit(
            data.replace(" ", KEEP).replace("\t", KEEP * 4)
            if self.pre
            else re.sub(r"\s+", " ", data)
        )


def _tidy(parts: list[str]) -> str:
    lines = [line.strip() for line in "".join(parts).split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip().replace(KEEP, " ")


def extract(html: str, base: str) -> tuple[str, str, list[str]]:
    """The page's title, its readable text and its links (absolute, without duplicates)."""
    p = _Extract(base)
    p.feed(html)
    p.close()
    main = _tidy(p.main_parts)
    use_main = len(main) >= MIN_MAIN
    text = main if use_main else _tidy(p.all)
    links = [u for u, in_main in p.links if in_main or not use_main]
    return " ".join(p.title.split()), text, list(dict.fromkeys(links))[:MAX_LINKS]
