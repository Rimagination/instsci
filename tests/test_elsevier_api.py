import unittest
from unittest.mock import patch

from instsci.sources import elsevier_api


class _FakeResponse:
    def __init__(self, status_code=200, *, text="", content=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}


class ElsevierApiXmlTests(unittest.TestCase):
    def test_parse_xml_extracts_namespaced_references(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <full-text-retrieval-response
            xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:ce="http://www.elsevier.com/xml/common/dtd"
            xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
          <coredata>
            <dc:title>Membrane fouling control</dc:title>
            <dc:creator>Ada Lovelace</dc:creator>
            <dc:description>This is the abstract.</dc:description>
          </coredata>
          <xocs:originalText>
            <xocs:doc>
              <xocs:body>
                <ce:section>
                  <ce:section-title>Introduction</ce:section-title>
                  <ce:para>First paragraph.</ce:para>
                </ce:section>
                <ce:bibliography>
                  <ce:bib-reference>
                    <ce:label>[1]</ce:label>
                    <ce:other-ref>Important cited work.</ce:other-ref>
                  </ce:bib-reference>
                </ce:bibliography>
              </xocs:body>
            </xocs:doc>
          </xocs:originalText>
        </full-text-retrieval-response>
        """

        parsed = elsevier_api._parse_xml(xml)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["title"], "Membrane fouling control")
        self.assertEqual(parsed["authors"], ["Ada Lovelace"])
        self.assertEqual(parsed["abstract"], "This is the abstract.")
        self.assertIn("## Introduction", parsed["full_text"])
        self.assertIn("First paragraph.", parsed["full_text"])
        self.assertEqual(parsed["references"], ["[1] Important cited work."])

    def test_extract_main_pdf_eid_filters_supplements(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <full-text-retrieval-response>
          <attachment type="MAIN" role="web-pdf">
            <attachment-eid>1-s2.0-S0043135424004093-main.pdf</attachment-eid>
          </attachment>
          <attachment type="supplementary-material">
            <attachment-eid>1-s2.0-S0043135424004093-mmc1.pdf</attachment-eid>
          </attachment>
        </full-text-retrieval-response>
        """

        self.assertEqual(
            elsevier_api._extract_main_pdf_eids(xml),
            ["1-s2.0-S0043135424004093-main.pdf"],
        )

    def test_extract_main_pdf_eid_can_infer_from_article_eid(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <full-text-retrieval-response>
          <coredata>
            <eid>1-s2.0-S0043135424004093</eid>
          </coredata>
        </full-text-retrieval-response>
        """

        self.assertEqual(
            elsevier_api._extract_main_pdf_eids(xml),
            ["1-s2.0-S0043135424004093-main.pdf"],
        )

    def test_fetch_fulltext_uses_full_view_and_direct_first_before_proxy(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <full-text-retrieval-response>
          <coredata><title>Route test</title></coredata>
          <attachment type="MAIN">
            <attachment-eid>1-s2.0-S0043135424004093-main.pdf</attachment-eid>
          </attachment>
        </full-text-retrieval-response>
        """
        responses = [
            _FakeResponse(403, text="NOT_ENTITLED", headers={"X-ELS-Status": "NOT_ENTITLED"}),
            _FakeResponse(200, text=xml),
        ]
        calls = []

        class FakeSession:
            def __init__(self):
                self.proxies = {}

            def get(self, url, **kwargs):
                calls.append((self.proxies.copy(), url, kwargs))
                return responses.pop(0)

        with patch("instsci.sources.elsevier_api.requests.Session", FakeSession):
            parsed = elsevier_api.fetch_fulltext(
                "10.1016/j.watres.2024.121507",
                "key",
                proxy_url="socks5://127.0.0.1:1080",
            )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["api_route"], "configured_proxy")
        self.assertEqual(calls[0][0], {})
        self.assertEqual(calls[0][2]["params"], {"view": "FULL"})
        self.assertEqual(calls[1][0]["https"], "socks5://127.0.0.1:1080")

    def test_fetch_pdf_uses_content_object_api_for_main_eid(self):
        pdf = b"%PDF-" + b"x" * 12000
        calls = []

        class FakeSession:
            def __init__(self):
                self.proxies = {}

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return _FakeResponse(
                    200,
                    content=pdf,
                    headers={"content-type": "application/pdf"},
                )

        with patch("instsci.sources.elsevier_api.requests.Session", FakeSession):
            result = elsevier_api.fetch_pdf(
                "10.1016/j.watres.2024.121507",
                "key",
                pdf_eids=["1-s2.0-S0043135424004093-main.pdf"],
            )

        self.assertEqual(result, pdf)
        self.assertIn("/content/object/eid/1-s2.0-S0043135424004093-main.pdf", calls[0][0])
        self.assertEqual(calls[0][1]["headers"]["Accept"], "application/pdf")


if __name__ == "__main__":
    unittest.main()
