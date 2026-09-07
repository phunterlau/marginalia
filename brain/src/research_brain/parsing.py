"""Dependency-light, evidence-preserving structural parsers."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import io
import re
import tarfile
from pathlib import Path
from dataclasses import replace

from .models import ParsedBlock


_DISPLAY_START = re.compile(r"^\s*(\$\$|\\\[|\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?)\})")
_DISPLAY_END = {
    "$$": re.compile(r"\$\$\s*$"),
    r"\[": re.compile(r"\\\]\s*$"),
}
_TEX_SECTION = re.compile(r"^\s*\\(title|part|chapter|section|subsection|subsubsection)\*?\{(.+)\}\s*$")
_TEX_INPUT = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _span_metadata(text: str, start: int, end: int) -> dict[str, int | str]:
    """Return exact offsets in the decoded member plus one-based line bounds."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return {
        "char_start": start,
        "char_end": end,
        "line_start": text.count("\n", 0, start) + 1,
        "line_end": text.count("\n", 0, max(start, end - 1)) + 1,
        "raw_sha256": __import__("hashlib").sha256(text[start:end].encode("utf-8")).hexdigest(),
    }


def _flush_paragraph(
    text: str,
    parts: list[tuple[str, int, int]],
    blocks: list[ParsedBlock],
    section: str | None,
) -> None:
    raw = "\n".join(part[0] for part in parts).strip()
    start = parts[0][1] if parts else 0
    end = parts[-1][2] if parts else 0
    parts.clear()
    if raw and not re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", raw):
        blocks.append(ParsedBlock("paragraph", raw, _normalize(raw), section_path=section,
                                  metadata=_span_metadata(text, start, end)))


def parse_text(text: str, *, markdown: bool = False, tex: bool = False) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    paragraph: list[tuple[str, int, int]] = []
    equation: list[tuple[str, int, int]] | None = None
    equation_end: re.Pattern[str] | None = None
    section: str | None = None

    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start, line_end = offset, offset + len(raw_line)
        offset = line_end
        if equation is not None:
            equation.append((line, line_start, line_end))
            if equation_end and equation_end.search(line):
                raw = "\n".join(item[0] for item in equation).strip()
                meta = _span_metadata(text, equation[0][1], equation[-1][2])
                blocks.append(ParsedBlock("equation", raw, _normalize(raw), section_path=section,
                                          raw_latex=raw, metadata=meta))
                equation = None
                equation_end = None
            continue

        tex_heading = _TEX_SECTION.match(line) if tex else None
        md_heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if markdown else None
        if tex_heading or md_heading:
            _flush_paragraph(text, paragraph, blocks, section)
            title = (tex_heading.group(2) if tex_heading else md_heading.group(2)).strip()
            section = title
            blocks.append(ParsedBlock("heading", line.strip(), _normalize(title), section_path=section,
                                      metadata=_span_metadata(text, line_start, line_end)))
            continue

        start = _DISPLAY_START.match(line)
        if start:
            _flush_paragraph(text, paragraph, blocks, section)
            token = start.group(1)
            if token.startswith(r"\begin"):
                env = re.search(r"\\begin\{([^}]+)\}", token).group(1)
                equation_end = re.compile(rf"\\end\{{{re.escape(env)}\}}\s*$")
            else:
                equation_end = _DISPLAY_END[token]
            equation = [(line, line_start, line_end)]
            # One-line display equations are common in Markdown and TeX.
            closes_on_same_line = (
                (token == "$$" and line.count("$$") >= 2)
                or (token != "$$" and equation_end.search(line[start.end():]) is not None)
            )
            if closes_on_same_line:
                raw = line.strip()
                blocks.append(ParsedBlock("equation", raw, _normalize(raw), section_path=section,
                                          raw_latex=raw, metadata=_span_metadata(text, line_start, line_end)))
                equation = None
                equation_end = None
            continue

        if not line.strip():
            _flush_paragraph(text, paragraph, blocks, section)
        else:
            paragraph.append((line, line_start, line_end))

    if equation is not None:
        raw = "\n".join(item[0] for item in equation).strip()
        meta = _span_metadata(text, equation[0][1], equation[-1][2])
        blocks.append(
            ParsedBlock(
                "equation",
                raw,
                _normalize(raw),
                section_path=section,
                raw_latex=raw,
                metadata={**meta, "unterminated": True},
            )
        )
    _flush_paragraph(text, paragraph, blocks, section)
    return blocks


class _HTMLBlockParser(HTMLParser):
    _BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "figcaption", "caption"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.active_tag: str | None = None
        self.buffer: list[str] = []
        self.section: str | None = None
        self.blocks: list[ParsedBlock] = []
        self.in_math = False
        self.math_parts: list[str] = []
        self.in_tex_annotation = False
        self.tex_parts: list[str] = []

    def _emit_text(self) -> None:
        raw = unescape("".join(self.buffer)).strip()
        self.buffer = []
        if not raw:
            return
        block_type = "heading" if (self.active_tag or "").startswith("h") else "paragraph"
        if block_type == "heading":
            self.section = _normalize(raw)
        self.blocks.append(ParsedBlock(block_type, raw, _normalize(raw), section_path=self.section))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw_tag = self.get_starttag_text() or f"<{tag}>"
        if self.in_math:
            self.math_parts.append(raw_tag)
            if tag == "annotation" and dict(attrs).get("encoding", "").lower() == "application/x-tex":
                self.in_tex_annotation = True
            return
        if tag == "math":
            self._emit_text()
            self.in_math = True
            self.math_parts = [raw_tag]
            self.tex_parts = []
            return
        if tag == "br" and self.active_tag:
            self.buffer.append("\n")
            return
        if tag in self._BLOCK_TAGS and self.active_tag is None:
            self.active_tag = tag
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.in_math:
            self.math_parts.append(data)
            if self.in_tex_annotation:
                self.tex_parts.append(data)
            return
        if self.active_tag:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_math:
            self.math_parts.append(f"</{tag}>")
            if tag == "annotation":
                self.in_tex_annotation = False
            if tag == "math":
                raw_mathml = "".join(self.math_parts)
                latex = unescape("".join(self.tex_parts)).strip() or None
                visible = _strip_tags(raw_mathml)
                self.blocks.append(
                    ParsedBlock(
                        "equation",
                        raw_mathml,
                        visible or (latex or ""),
                        section_path=self.section,
                        raw_latex=latex,
                        metadata={"format": "mathml"},
                    )
                )
                self.in_math = False
                self.math_parts = []
                self.tex_parts = []
            return
        if tag != self.active_tag:
            return
        self._emit_text()
        self.active_tag = None
        self.buffer = []

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")


def _strip_tags(value: str) -> str:
    return _normalize(unescape(re.sub(r"<[^>]+>", " ", value)))


def parse_html(text: str) -> list[ParsedBlock]:
    parser = _HTMLBlockParser()
    parser.feed(text)
    parser._emit_text()
    return parser.blocks


def parse_pdf(data: bytes) -> list[ParsedBlock]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("PDF parsing requires the pypdf dependency; reinstall research-brain") from exc

    blocks: list[ParsedBlock] = []
    reader = PdfReader(io.BytesIO(data))
    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        for parsed in parse_text(text):
            blocks.append(
                ParsedBlock(
                    parsed.block_type,
                    parsed.raw_text,
                    parsed.normalized_text,
                    section_path=parsed.section_path,
                    page=page_number,
                    raw_latex=parsed.raw_latex,
                    metadata=parsed.metadata,
                )
            )
    return blocks


def _safe_tex_members(
    data: bytes,
    *,
    max_member_bytes: int = 32 * 1024 * 1024,
    max_total_bytes: int = 128 * 1024 * 1024,
    max_members: int = 10_000,
) -> dict[str, bytes]:
    """Read TeX members without extracting an untrusted archive to disk."""
    members: dict[str, bytes] = {}
    # Bound the complete decompressed stream, including unused members/headers,
    # before tarfile can traverse it. This also covers compressed single TeX.
    if data.startswith(b"\x1f\x8b"):
        import gzip
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            data = compressed.read(max_total_bytes + 1)
    if len(data) > max_total_bytes:
        raise RuntimeError("arXiv source exceeds decompressed size limit")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.ReadError:
        # Some arXiv submissions are a single, optionally gzip-compressed TeX file.
        import gzip

        try:
            if data.startswith(b"\x1f\x8b"):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    unpacked = compressed.read(max_total_bytes + 1)
            else:
                unpacked = data
        except (gzip.BadGzipFile, OSError, EOFError) as exc:
            raise ValueError("arXiv source payload is neither a safe tar archive nor TeX") from exc
        if len(unpacked) > max_total_bytes:
            raise ValueError("arXiv source payload exceeds the decompressed size limit")
        if b"\\document" not in unpacked and b"\\section" not in unpacked:
            raise ValueError("arXiv source payload does not contain recognizable TeX")
        return {"main.tex": unpacked}

    with archive:
        total_bytes = 0
        for member_index, member in enumerate(archive):
            if member_index >= max_members:
                raise RuntimeError("arXiv source exceeds archive member limit")
            total_bytes += member.size
            if total_bytes > max_total_bytes:
                raise RuntimeError("arXiv source exceeds decompressed size limit")
            if not member.isfile() or member.size > max_member_bytes:
                continue
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                continue
            if member_path.suffix.lower() not in {".tex", ".ltx"}:
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                members[member_path.as_posix()] = extracted.read(max_member_bytes + 1)
    if not members:
        raise ValueError("arXiv source archive contains no TeX files")
    return members


def _uncomment_tex(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*", "", line) for line in text.splitlines())


def _tex_document_order(members: dict[str, bytes], *, main_member: str | None = None) -> list[str]:
    decoded = {name: data.decode("utf-8", errors="replace") for name, data in members.items()}
    if main_member is not None:
        normalized = Path(main_member).as_posix().lstrip("./")
        if normalized not in decoded:
            raise ValueError(f"declared main TeX member is missing: {main_member}")
        main = normalized
    else:
        uncommented = {name: _uncomment_tex(text) for name, text in decoded.items()}
        candidates = [name for name, text in uncommented.items()
                      if r"\documentclass" in text or r"\documentstyle" in text]
        main = min(
            candidates or decoded.keys(),
            key=lambda name: (
                name.count("/"),
                r"\begin{document}" not in uncommented.get(name, ""),
                Path(name).stem.lower() not in {"main", "paper", "manuscript", "submission"},
                len(name),
                name,
            ),
        )
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen or name not in decoded:
            return
        seen.add(name)
        ordered.append(name)
        base = Path(name).parent
        for referenced in _TEX_INPUT.findall(decoded[name]):
            clean = referenced.strip().replace("\\", "/")
            candidates = [clean, (base / clean).as_posix()]
            choices = [choice for candidate in candidates for choice in
                       (candidate, f"{candidate}.tex", f"{candidate}.ltx")]
            match = next((choice for choice in choices if choice in decoded), None)
            if match:
                visit(match)

    visit(main)
    return ordered


def parse_arxiv_source(data: bytes, *, main_member: str | None = None) -> list[ParsedBlock]:
    members = _safe_tex_members(data)
    blocks: list[ParsedBlock] = []
    decoded = {name: value.decode("utf-8", errors="replace") for name, value in members.items()}
    main = _tex_document_order(members, main_member=main_member)[0]
    seen: set[str] = set()

    def resolve_reference(name: str, referenced: str) -> str | None:
        base = Path(name).parent
        clean = referenced.strip().replace("\\", "/")
        candidates = [clean, (base / clean).as_posix()]
        choices = [choice for candidate in candidates for choice in
                   (candidate, f"{candidate}.tex", f"{candidate}.ltx")]
        return next((choice for choice in choices if choice in decoded), None)

    def append_segment(name: str, segment: str, base_offset: int) -> None:
        for block in parse_text(segment, tex=True):
            metadata = {**(block.metadata or {}), "source_member": name}
            if "char_start" in metadata:
                metadata["char_start"] = int(metadata["char_start"]) + base_offset
                metadata["char_end"] = int(metadata["char_end"]) + base_offset
                original = decoded[name]
                metadata["line_start"] = original.count("\n", 0, int(metadata["char_start"])) + 1
                metadata["line_end"] = original.count("\n", 0, max(int(metadata["char_start"]), int(metadata["char_end"]) - 1)) + 1
            blocks.append(replace(block, metadata=metadata))

    def visit(name: str, *, is_main: bool = False) -> None:
        if name in seen:
            return
        seen.add(name)
        original = decoded[name]
        content = original
        base_offset = 0
        if is_main:
            preamble, marker, body = original.partition(r"\begin{document}")
            if marker:
                title = re.search(r"\\title\s*\{(.+?)\}", preamble, flags=re.DOTALL)
                if title:
                    title_text = _normalize(re.sub(r"(?<!\\)[{}]", "", title.group(1)))
                    title_meta = _span_metadata(original, title.start(), title.end())
                    blocks.append(ParsedBlock("heading", title.group(0), title_text, section_path=title_text,
                                              metadata={**title_meta, "source_member": name, "tex_command": "title"}))
                base_offset = len(preamble) + len(marker)
                content = body.rsplit(r"\end{document}", 1)[0]
        cursor = 0
        for match in _TEX_INPUT.finditer(content):
            append_segment(name, content[cursor:match.start()], base_offset + cursor)
            referenced = resolve_reference(name, match.group(1))
            if referenced:
                visit(referenced)
            else:
                # Missing includes are diagnostics-worthy source evidence, not fatal corruption.
                missing = match.group(0)
                meta = _span_metadata(original, base_offset + match.start(), base_offset + match.end())
                blocks.append(ParsedBlock("missing_include", missing, _normalize(missing),
                                          metadata={**meta, "source_member": name,
                                                    "missing_reference": match.group(1)}))
            cursor = match.end()
        append_segment(name, content[cursor:], base_offset + cursor)

    visit(main, is_main=True)
    return blocks


def parse_document(data: bytes, *, name: str, content_type: str | None = None) -> list[ParsedBlock]:
    suffix = Path(name.split("?", 1)[0]).suffix.lower()
    media_type = (content_type or "").split(";", 1)[0].lower()
    if suffix == ".pdf" or media_type == "application/pdf":
        return parse_pdf(data)
    text = data.decode("utf-8", errors="replace")
    if suffix in {".html", ".htm"} or media_type in {"text/html", "application/xhtml+xml"}:
        return parse_html(text)
    if suffix in {".tex", ".latex"} or media_type in {"application/x-tex", "text/x-tex"}:
        return parse_text(text, tex=True)
    return parse_text(text, markdown=suffix in {".md", ".markdown"})
