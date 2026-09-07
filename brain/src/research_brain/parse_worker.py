"""Isolated structural compilation; no model SDK, network or TeX execution."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .models import ParsedBlock
from .parsing import parse_arxiv_source, parse_document


MAX_OUTPUT_BYTES = 256 * 1024 * 1024


def compile_source(data: bytes, *, name: str, kind: str, content_type: str | None = None,
                   main_member: str | None = None, timeout: float = 120) -> tuple[list[ParsedBlock], dict]:
    if not 0 < timeout <= 120:
        raise ValueError("Parser timeout must be in (0, 120] seconds")
    if len(data) > 50 * 1024 * 1024:
        raise RuntimeError("Source exceeds 50 MiB input limit")
    with tempfile.TemporaryDirectory(prefix="brain-parse-") as temporary:
        root = Path(temporary)
        (root / "source").write_bytes(data)
        (root / "config.json").write_text(json.dumps(dict(name=name, kind=kind, content_type=content_type, main_member=main_member)))
        env = {"PATH": os.environ.get("PATH", ""),
               "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        with (root / "result.json").open("wb") as output, (root / "stderr").open("wb") as error:
            try:
                process = subprocess.run([sys.executable, "-c", "from research_brain.parse_worker import main; raise SystemExit(main())", str(root)],
                                         env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=error, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                # subprocess.run kills and reaps the child before returning.
                raise RuntimeError("Structural parser exceeded its deadline; no source committed") from exc
        if (root / "result.json").stat().st_size > MAX_OUTPUT_BYTES:
            raise RuntimeError("Parser output exceeds size limit")
        if process.returncode < 0:
            raise RuntimeError("Structural parser terminated or exceeded a resource limit")
        try:
            result = json.loads((root / "result.json").read_text())
        except ValueError as exc:
            raise RuntimeError("Structural parser returned invalid output") from exc
        if process.returncode:
            exception = RuntimeError if result.get("resource_error") else ValueError
            raise exception(result.get("error", "Structural parser failed"))
        return [ParsedBlock(**block) for block in result["blocks"]], result["diagnostics"]


def main() -> int:
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 121))
    root = Path(sys.argv[1])
    config = json.loads((root / "config.json").read_text())
    data = (root / "source").read_bytes()
    diagnostics = {"source_kind": config["kind"], "main_tex": config["main_member"]}
    try:
        if config["kind"] == "source_archive":
            blocks = parse_arxiv_source(data, main_member=config["main_member"], diagnostics=diagnostics)
        else:
            blocks = parse_document(data, name=config["name"], content_type=config["content_type"])
        diagnostics.update(block_count=len(blocks), equation_count=sum(b.block_type == "equation" for b in blocks),
                           comment_blocks=sum(b.block_type == "comment" for b in blocks), parser_deadline_seconds=120)
        print(json.dumps({"blocks": [asdict(b) for b in blocks], "diagnostics": diagnostics}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "resource_error": isinstance(exc, (RuntimeError, MemoryError))}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
