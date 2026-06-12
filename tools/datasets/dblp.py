"""DBLP titles + authors extractor — ODC-BY.

Upstream: https://dblp.org/xml/dblp.xml.gz
Streaming SAX parse; each publication record contributes one row per <title>
and one row per <author> child. Multi-author records emit multiple author rows;
the per-record correspondence between titles and authors is not preserved
(headline use is per-string distribution, not relational join).
"""
from __future__ import annotations
import gzip
import xml.sax
from pathlib import Path

from ._common import download_if_missing, write_rows


# Top-level publication element names per DBLP DTD.
_PUB_ELEMENTS = {"article", "inproceedings", "proceedings", "book", "incollection",
                 "phdthesis", "mastersthesis", "www", "data"}


class _DBLPHandler(xml.sax.ContentHandler):
    def __init__(self) -> None:
        super().__init__()
        self._buf: list[str] = []
        self._capturing: str | None = None  # "title" or "author" or None
        self.titles: list[bytes] = []
        self.authors: list[bytes] = []

    def startElement(self, name, attrs) -> None:  # noqa: N802
        if name == "title" or name == "author":
            self._capturing = name
            self._buf = []

    def endElement(self, name) -> None:  # noqa: N802
        if name == "title" and self._capturing == "title":
            text = "".join(self._buf).strip()
            if text:
                self.titles.append(text.encode("utf-8"))
            self._capturing = None
            self._buf = []
        elif name == "author" and self._capturing == "author":
            text = "".join(self._buf).strip()
            if text:
                self.authors.append(text.encode("utf-8"))
            self._capturing = None
            self._buf = []

    def characters(self, content) -> None:
        if self._capturing is not None:
            self._buf.append(content)


def download(target_dir: Path, manifest_entry: dict) -> Path:
    target = target_dir / "dblp.xml.gz"
    return download_if_missing(
        manifest_entry["source"]["url"], target,
        expected_sha256=manifest_entry["source"]["sha256"],
    )


def _open_xml(raw_path: Path):
    if raw_path.suffix == ".gz":
        return gzip.open(str(raw_path), "rb")
    return open(raw_path, "rb")


def extract_columns(raw_path: Path, output_dir: Path, manifest_entry: dict,
                    subset_params: dict | None = None) -> dict[str, Path]:
    handler = _DBLPHandler()
    parser = xml.sax.make_parser()
    parser.setFeature(xml.sax.handler.feature_external_ges, False)  # don't fetch DTD
    parser.setContentHandler(handler)
    with _open_xml(raw_path) as f:
        parser.parse(f)

    titles_out  = output_dir / "titles.col"
    authors_out = output_dir / "authors.col"
    write_rows(titles_out,  handler.titles)
    write_rows(authors_out, handler.authors)
    return {"titles": titles_out, "authors": authors_out}
