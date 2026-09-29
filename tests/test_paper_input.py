import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from typer.testing import CliRunner

from instsci.cli import app
from instsci.config import Config
from instsci.paper_input import extract_doi, read_paper_input, write_publisher_queue
from instsci.publisher_profiles import infer_publisher_profile


class PaperInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, text, name="papers.txt"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_doi_urls_and_citation_wrappers_preserve_suffix(self):
        cases = {
            "https://doi.org/10.1002%2FAbC?download=true#page": "10.1002/AbC",
            "Author (2026). Title. https://doi.org/10.1002/AbC.": "10.1002/AbC",
            "(doi:10.1002/test(2026))": "10.1002/test(2026)",
            "10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P":
                "10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P",
            "10.1002/AbC 10.1002/Other": "",
            "References: 10.1002/a;10.1002/b": "",
            "[10.1002/abc](https://doi.org/10.1002/abc)": "",
            "A title without a DOI": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(extract_doi(raw), expected)

    def test_bom_comments_dedup_and_unresolved_accounting(self):
        path = self.source("\ufeff# comment\n\n10.1002/AbC\nhttps://doi.org/10.1002/abc\nmissing\n")
        result = read_paper_input(path)
        self.assertEqual(result.total, 3)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual([r.doi for r in result.records], ["10.1002/AbC"])
        self.assertEqual(result.unresolved[0]["line"], 5)
        self.assertEqual(result.unresolved[0]["cells"], ["missing"])

    def test_quoted_csv_metadata_and_duplicate_enrichment(self):
        path = self.source(
            'DOI,标题,年份,url\n10.1002/ABC,,,\n'
            '10.1002/abc,"A title, with\na newline",2026,https://example.org/article\n',
            "papers.csv",
        )
        result = read_paper_input(path)
        self.assertEqual(result.duplicates, 1)
        record = result.records[0]
        self.assertEqual(record.title.splitlines(), ["A title, with", "a newline"])
        self.assertEqual(record.published, "2026")
        self.assertEqual(record.url, "https://example.org/article")

    def test_tsv_header_and_headerless_single_row(self):
        for text in (
            "identifier\tchannel\toa_url\n10.1002/ABC\toa\thttps://example.org/p.pdf\n",
            "10.1002/ABC\toa\thttps://example.org/p.pdf\n",
        ):
            with self.subTest(text=text):
                result = read_paper_input(self.source(text, "papers.tsv"))
                self.assertEqual(result.records[0].doi, "10.1002/ABC")
                self.assertEqual(result.total, 1)
                self.assertFalse(result.unresolved)

    def test_invalid_file_formats_and_malformed_csv_are_actionable(self):
        cases = [("x.xlsx", "text"), ("x.csv", 'doi,title\n10.1002/a,"unfinished')]
        for name, content in cases:
            with self.subTest(name=name):
                result = CliRunner().invoke(app, ["queue", str(self.source(content, name))])
                self.assertEqual(result.exit_code, 1)
                self.assertIn("Cannot read paper input", result.output)

    def test_duplicate_headers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate input columns"):
            read_paper_input(self.source("doi,identifier\n10.1002/a,10.1002/b\n", "x.csv"))

    def test_wrong_column_count_preserves_the_entire_row(self):
        result = read_paper_input(self.source("doi,title\n10.1002/a,Title,extra\n", "x.csv"))
        self.assertFalse(result.records)
        self.assertEqual(result.unresolved[0]["cells"], ["10.1002/a", "Title", "extra"])
        self.assertEqual(result.unresolved[0]["reason"], "column_count_mismatch")

    def test_hash_prefixed_table_cells_are_not_comments(self):
        result = read_paper_input(self.source(
            "# preamble\ntitle,doi\n#1 research priority,10.1002/abc\n# unknown,missing\n", "x.csv"
        ))
        self.assertEqual(result.total, 2)
        self.assertEqual(result.records[0].title, "#1 research priority")
        self.assertEqual(result.records[0].doi, "10.1002/abc")
        self.assertEqual(result.unresolved[0]["cells"], ["# unknown", "missing"])

    def test_publisher_inference_requires_exact_registrant(self):
        self.assertEqual(infer_publisher_profile("10.1038/example").name, "Springer Nature")
        self.assertIsNone(infer_publisher_profile("10.10380/example"))

    def test_queue_roundtrip_preserves_records_and_accounts_for_all_rows(self):
        path = self.source(
            'doi,title\n10.1021/a,"A, title"\n10.1016/b,B\n10.1021/A,Duplicate\n'
            '10.9999/c,C\nmissing,Keep this row\n', "papers.csv"
        )
        parsed = read_paper_input(path)
        directory = self.root / "queue"
        manifest = write_publisher_queue(parsed, directory)
        self.assertEqual(manifest, json.loads((directory / "queue.json").read_text(encoding="utf-8")))
        self.assertEqual(manifest["counts"], {
            "input_rows": 5, "unique_dois": 3, "duplicates": 1,
            "unresolved": 1, "unknown_publisher": 1,
        })
        self.assertEqual(manifest["unresolved"][0]["cells"], ["missing", "Keep this row"])
        self.assertEqual([g["publisher"] for g in manifest["groups"]], ["acs", "elsevier", "unknown"])
        recovered = []
        for group in manifest["groups"]:
            recovered.extend(read_paper_input(Path(group["input_file"])).records)
            if group["publisher"] == "unknown":
                self.assertNotIn("argv", group)
            else:
                self.assertEqual(group["argv"][1], "papers")
                self.assertEqual(group["argv"][4], group["publisher"])
        self.assertEqual(recovered, parsed.records)

    def test_queue_does_not_overwrite_an_existing_directory(self):
        sentinel = self.source("keep me", "queue.json")
        parsed = read_paper_input(self.source("10.1002/a\n"))
        with self.assertRaises(FileExistsError):
            write_publisher_queue(parsed, self.root)
        self.assertEqual(sentinel.read_text(), "keep me")

    def test_queue_is_offline_and_needs_no_institution(self):
        source = self.source("10.1021/a\nnot resolved\n")
        with patch("instsci.cli.Config.load", side_effect=AssertionError("must stay offline")), \
             patch("instsci.cli.PaperFetcher", side_effect=AssertionError("must not fetch")), \
             patch("instsci.cli._ensure_session_broker", side_effect=AssertionError("must not start broker")):
            result = CliRunner().invoke(app, ["queue", str(source), "-o", str(self.root / "planned")])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.root / "planned" / "acs.csv").is_file())

    def test_all_download_commands_reject_unresolved_rows_before_configuration(self):
        source = self.source("doi,title\nmissing,Keep this row\n", "papers.csv")
        for command in ("batch", "papers", "publisher-batch"):
            with self.subTest(command=command), \
                 patch("instsci.cli.Config.load", side_effect=AssertionError("must validate first")):
                result = CliRunner().invoke(app, [command, str(source)])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Unresolved input rows", result.output)

    def test_browser_commands_submit_normalized_metadata_once(self):
        source = self.source("doi,title,year\nhttps://doi.org/10.1021/ABC,Title,2026\n10.1021/abc,,\n", "papers.csv")
        config = Config(carsi_idp_name="Example University")
        summary = {"success": 1, "missing": 0, "unverified": 0,
                   "pdf_dir": "pdfs", "manifest": "manifest.csv", "verified_match": 1}
        for command in ("papers", "publisher-batch"):
            with self.subTest(command=command), \
                 patch("instsci.cli.Config.load", return_value=config), \
                 patch("instsci.cli._ensure_session_broker", return_value=True), \
                 patch("instsci.session_broker.submit_broker_job", return_value=summary) as submit:
                result = CliRunner().invoke(app, [command, str(source), "-p", "acs"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(submit.call_args.kwargs["records"], [
                    {"doi": "10.1021/ABC", "title": "Title", "published": "2026", "url": ""}
                ])


if __name__ == "__main__":
    unittest.main()
