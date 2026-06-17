"""Tests for CSV parsing support in the DocReader service."""

import sys
from pathlib import Path

import pytest

# Ensure docreader module is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "docreader"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from mantisfetch_docreader import parse_csv


@pytest.fixture()
def sample_csv(tmp_path: Path) -> Path:
    """Create a minimal CSV file."""
    path = tmp_path / "sales.csv"
    path.write_text("Region,Q1,Q2\nNorth,100,200\nSouth,150,250\n", encoding="utf-8")
    return path


def test_csv_parse_returns_parsed_document(sample_csv: Path) -> None:
    """parse_csv returns a ParsedDocument with correct metadata."""
    result = parse_csv(sample_csv)

    assert result.file_type == "csv"
    assert result.filename == "sales.csv"
    assert result.total_pages == 1


def test_csv_single_section(sample_csv: Path) -> None:
    """The entire CSV file becomes one section named after the file stem."""
    result = parse_csv(sample_csv)

    assert len(result.sections) == 1
    assert result.sections[0].title == "sales"


def test_csv_section_text_contains_data(sample_csv: Path) -> None:
    """Section text contains the CSV data."""
    result = parse_csv(sample_csv)

    text = result.sections[0].text
    assert "Region" in text
    assert "North" in text


def test_csv_table_count(sample_csv: Path) -> None:
    """table_count is 1 for a non-empty CSV."""
    result = parse_csv(sample_csv)

    assert result.table_count >= 1


def test_csv_section_has_stable_sid(sample_csv: Path) -> None:
    """The section has a non-empty stable ID."""
    result = parse_csv(sample_csv)

    assert result.sections[0].sid


def test_csv_utf8_bom_encoding(tmp_path: Path) -> None:
    """CSV files with UTF-8-BOM encoding (common Excel export) are parsed correctly."""
    path = tmp_path / "bom.csv"
    path.write_bytes("Category,Value\nRent,5000\n".encode("utf-8-sig"))

    result = parse_csv(path)
    assert len(result.sections) == 1
    text = result.sections[0].text
    assert "Category" in text


def test_csv_empty_file(tmp_path: Path) -> None:
    """An empty CSV produces no sections and table_count=0."""
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    result = parse_csv(path)
    assert len(result.sections) == 0
    assert result.table_count == 0
