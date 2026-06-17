from pathlib import Path

from services.docreader.mantisfetch_docreader import parse_generic


def test_parse_text_json_jsonl_xml_formats(tmp_path: Path) -> None:
    samples = {
        "sample.txt": "plain text\n",
        "sample.json": '{"name":"demo","value":1}',
        "sample.jsonl": '{"row":1}\n{"row":2}\n',
        "sample.xml": "<root><item>demo</item></root>",
    }

    for filename, content in samples.items():
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")

        result = parse_generic(path)

        assert result.file_type == path.suffix.lstrip(".")
        assert result.pages
        assert result.sections
        assert content.strip().splitlines()[0] in result.pages[0].text
