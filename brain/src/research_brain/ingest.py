"""Source resolution, immutable archival, and structural compilation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mimetypes
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from .ids import stable_id
from .models import IngestResult
from .parsing import parse_arxiv_source, parse_document
from .store import SQLiteStore
from .store.sqlite import utc_now


PARSER_NAME = "structural"
PARSER_VERSION = "structural-v3"
PARSER_CONFIG_DIGEST = hashlib.sha256(b"in-place-reachable-tex-includes;decoded-char-spans;v3").hexdigest()
_ARXIV_ID = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/)(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedSource:
    data: bytes
    uri: str
    name: str
    content_type: str | None
    kind: str
    canonical_document_uri: str
    version_label: str | None
    external_ids: dict[str, str]
    requested_uri: str | None = None
    resolution_state: str = "resolved"
    license_uri: str | None = None


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path) or "/"
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def arxiv_identity(url: str) -> tuple[str, str | None] | None:
    match = _ARXIV_ID.search(url)
    if not match:
        return None
    return match.group(1), match.group(2)


def _kind(name: str, content_type: str | None) -> str:
    suffix = Path(name.split("?", 1)[0]).suffix.lower()
    media_type = (content_type or "").split(";", 1)[0].lower()
    if suffix == ".pdf" or media_type == "application/pdf":
        return "pdf"
    if suffix in {".tex", ".latex"} or "tex" in media_type:
        return "tex"
    if suffix in {".html", ".htm"} or "html" in media_type:
        return "html"
    return "text"


def _download(url: str, *, timeout: float) -> tuple[bytes, str, str | None]:
    request = Request(url, headers={"User-Agent": "research-brain/0.1 (+evidence archive)"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - caller explicitly requested URL ingestion
        return response.read(), canonicalize_url(response.geturl()), response.headers.get_content_type()


def _looks_like_tex_source(data: bytes, content_type: str | None) -> bool:
    if data.startswith(b"%PDF-") or (content_type or "").lower() == "application/pdf":
        return False
    if data.startswith((b"\x1f\x8b", b"ustar")):
        return True
    sample = data[:8192].lower()
    return b"\\document" in sample or b"\\section" in sample


def _arxiv_metadata(base_id: str, *, timeout: float) -> tuple[str | None, str | None]:
    """Resolve the current revision and license from the canonical abstract page."""
    try:
        data, final_url, _ = _download(f"https://arxiv.org/abs/{base_id}", timeout=timeout)
    except (OSError, HTTPError):
        return None, None
    text = data.decode("utf-8", errors="replace")
    versions = [int(value) for value in re.findall(rf"{re.escape(base_id)}v(\d+)", text)]
    final_identity = arxiv_identity(final_url)
    if final_identity and final_identity[1]:
        versions.append(int(final_identity[1][1:]))
    license_match = re.search(r'href=["\'](https?://(?:creativecommons\.org|arxiv\.org)/[^"\']+)["\'][^>]*rel=["\']license', text, re.I)
    return (f"v{max(versions)}" if versions else None), (license_match.group(1) if license_match else None)


def resolve_source(source: str | Path, *, timeout: float = 30.0) -> ResolvedSource:
    source_text = str(source)
    if source_text.startswith(("http://", "https://")):
        requested_url = canonicalize_url(source_text)
        arxiv = arxiv_identity(requested_url)
        if arxiv:
            base_id, version = arxiv
            resolved_version = version
            license_uri = None
            resolution_state = "resolved"
            if version is None:
                resolved_version, license_uri = _arxiv_metadata(base_id, timeout=timeout)
                if resolved_version is None:
                    resolution_state = "unresolved"
            requested_version = resolved_version or ""
            source_url = f"https://arxiv.org/src/{base_id}{requested_version}"
            use_pdf_fallback = False
            try:
                data, final_url, content_type = _download(source_url, timeout=timeout)
                if not _looks_like_tex_source(data, content_type):
                    raise ValueError("arXiv source endpoint did not return a TeX source package")
                # Validate now so malformed/non-TeX payloads use the PDF fallback.
                parse_arxiv_source(data)
                name = f"{base_id}{requested_version}.tar.gz"
                kind = "source_archive"
            except HTTPError as exc:
                # Missing/forbidden source can legitimately fall back. Rate limits and
                # server errors are transient and should remain visible to the caller.
                if exc.code not in {400, 403, 404}:
                    raise
                use_pdf_fallback = True
            except ValueError:
                use_pdf_fallback = True
            if use_pdf_fallback:
                pdf_url = f"https://arxiv.org/pdf/{base_id}{requested_version}.pdf"
                data, final_url, content_type = _download(pdf_url, timeout=timeout)
                if not data.startswith(b"%PDF-") and (content_type or "").lower() != "application/pdf":
                    raise ValueError("arXiv PDF endpoint did not return a PDF")
                name = f"{base_id}{requested_version}.pdf"
                kind = "pdf"
            canonical_document_uri = f"https://arxiv.org/abs/{base_id}"
            external_ids = {"arxiv": base_id}
            version_label = resolved_version
        else:
            data, final_url, content_type = _download(requested_url, timeout=timeout)
            name = Path(urlparse(final_url).path).name or "download"
            kind = _kind(name, content_type)
            canonical_document_uri = requested_url
            external_ids = {}
            version_label = None
            license_uri = None
            resolution_state = "resolved"
        return ResolvedSource(data, final_url, name, content_type, kind, canonical_document_uri,
                              version_label, external_ids, requested_url, resolution_state, license_uri)

    path = Path(source_text).expanduser().resolve(strict=True)
    data = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0]
    uri = path.as_uri()
    return ResolvedSource(data, uri, path.name, content_type, _kind(path.name, content_type),
                          uri, None, {}, uri, "resolved", None)


class Ingestor:
    def __init__(self, store: SQLiteStore, asset_root: str | Path):
        self.store = store
        self.asset_root = Path(asset_root)
        self.asset_root.mkdir(parents=True, exist_ok=True)

    def ingest(self, source: str | Path) -> IngestResult:
        resolved = resolve_source(source)
        return self._ingest_resolved(resolved)

    def ingest_manifest(self, manifest_path: str | Path) -> IngestResult:
        manifest_path = Path(manifest_path).expanduser().resolve(strict=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        required = {"arxiv_id", "version", "source_url", "source_file", "sha256"}
        if not isinstance(manifest, dict) or not required <= manifest.keys():
            raise ValueError(f"Invalid paper fixture manifest: {manifest_path}")
        source_path = (manifest_path.parent / manifest["source_file"]).resolve(strict=True)
        data = source_path.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != manifest["sha256"]:
            raise ValueError(f"Fixture checksum mismatch: {source_path}")
        base_id = str(manifest["arxiv_id"])
        version = str(manifest["version"])
        resolved = ResolvedSource(
            data=data, uri=str(manifest["source_url"]), name=source_path.name,
            content_type="application/gzip", kind="source_archive",
            canonical_document_uri=f"https://arxiv.org/abs/{base_id}", version_label=version,
            external_ids={"arxiv": base_id}, requested_uri=str(manifest["source_url"]),
            resolution_state="resolved", license_uri=manifest.get("license_uri"),
        )
        return self._ingest_resolved(resolved)

    def _ingest_resolved(self, resolved: ResolvedSource) -> IngestResult:
        sha256 = hashlib.sha256(resolved.data).hexdigest()
        asset_id = stable_id("asset", resolved.uri, sha256)
        document_id = stable_id("doc", resolved.canonical_document_uri)
        version_id = stable_id("version", document_id, sha256)
        compilation_id = stable_id("comp", version_id, PARSER_NAME, PARSER_VERSION, PARSER_CONFIG_DIGEST)
        suffix = ".tar.gz" if resolved.name.lower().endswith(".tar.gz") else Path(resolved.name).suffix.lower()
        archive_path = self.asset_root / sha256[:2] / f"{sha256}{suffix}"
        self._archive_once(archive_path, resolved.data)

        blocks = (
            parse_arxiv_source(resolved.data)
            if resolved.kind == "source_archive"
            else parse_document(resolved.data, name=resolved.name, content_type=resolved.content_type)
        )
        title = next((block.normalized_text for block in blocks if block.block_type == "heading"), None)
        now = utc_now()
        created_version, created_compilation = self.store.ingest_compilation(
            asset={"id": asset_id, "kind": resolved.kind, "uri": resolved.uri,
                   "requested_uri": resolved.requested_uri, "local_path": str(archive_path),
                   "sha256": sha256, "content_type": resolved.content_type,
                   "retrieved_at": now, "license_uri": resolved.license_uri, "created_at": now},
            document={"id": document_id, "canonical_url": resolved.canonical_document_uri,
                      "title": title, "external_ids": resolved.external_ids, "created_at": now},
            version={"id": version_id, "version_label": resolved.version_label,
                     "resolution_state": resolved.resolution_state, "created_at": now},
            compilation={"id": compilation_id, "parser_name": PARSER_NAME,
                         "parser_version": PARSER_VERSION, "config_digest": PARSER_CONFIG_DIGEST,
                         "diagnostics": {"source_kind": resolved.kind}, "created_at": now},
            blocks=blocks,
        )
        return IngestResult(document_id, version_id, asset_id, sha256, len(blocks),
                            created_version, compilation_id, created_compilation)

    @staticmethod
    def _archive_once(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
                raise RuntimeError(f"Content-addressed asset collision at {path}")
            return
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
