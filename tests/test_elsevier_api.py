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

    def test_extract_supplementary_object_refs_uses_structured_mmc_metadata(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <full-text-retrieval-response>
          <objects>
            <object category="main" type="MAIN" mimetype="application/pdf" size="900000">
              <ref>https://api.elsevier.com/content/object/eid/1-s2.0-S0043135424004093-main.pdf</ref>
            </object>
            <object category="mmc1" type="standard" mimetype="application/excel" size="60299">
              <ref>https://api.elsevier.com/content/object/eid/1-s2.0-S0043135424004093-mmc1.xlsx</ref>
            </object>
            <object category="thumbnail" type="image" mimetype="image/jpeg" size="1200">
              <ref>https://api.elsevier.com/content/object/eid/1-s2.0-S0043135424004093-gr1.jpg</ref>
            </object>
          </objects>
        </full-text-retrieval-response>
        """

        refs = elsevier_api.extract_supplementary_object_refs(xml)

        self.assertEqual([ref.eid for ref in refs], ["1-s2.0-S0043135424004093-mmc1.xlsx"])
        self.assertEqual(refs[0].content_type, "application/excel")
        self.assertEqual(refs[0].size_bytes, 60299)
        self.assertIn("mmc1", refs[0].label)

    def test_fetch_supplementary_objects_downloads_assets_by_eid_name(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <full-text-retrieval-response>
          <objects>
            <object category="mmc1" type="standard" mimetype="application/excel" size="12">
              <ref>https://api.elsevier.com/content/object/eid/1-s2.0-S0043135424004093-mmc1.xlsx</ref>
            </object>
            <object category="supplementary-material" type="standard" mimetype="application/pdf" size="12000">
              <ref>https://api.elsevier.com/content/object/eid/1-s2.0-S0043135424004093-mmc2.pdf</ref>
            </object>
          </objects>
        </full-text-retrieval-response>
        """
        calls = []

        class FakeSession:
            def __init__(self):
                self.proxies = {}

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                if "/article/doi/" in url:
                    return _FakeResponse(
                        200,
                        text=xml,
                        headers={"content-type": "application/xml;charset=UTF-8"},
                    )
                if "mmc1.xlsx" in url:
                    return _FakeResponse(
                        200,
                        content=b"PK\x03\x04" + b"xlsx-body" * 8,
                        headers={
                            "content-type": "application/excel;charset=UTF-8",
                            "content-disposition": "attachment; filename=f.txt",
                        },
                    )
                return _FakeResponse(
                    200,
                    content=b"%PDF-" + b"x" * 12000,
                    headers={"content-type": "application/pdf;charset=UTF-8"},
                )

        with patch("instsci.sources.elsevier_api.requests.Session", FakeSession):
            objects = elsevier_api.fetch_supplementary_objects(
                "10.1016/j.watres.2024.121507",
                "key",
            )

        self.assertEqual([obj.filename for obj in objects], [
            "1-s2.0-S0043135424004093-mmc1.xlsx",
            "1-s2.0-S0043135424004093-mmc2.pdf",
        ])
        self.assertTrue(objects[0].data.startswith(b"PK\x03\x04"))
        self.assertEqual(objects[1].data[:5], b"%PDF-")
        object_calls = [call for call in calls if "/content/object/eid/" in call[0]]
        self.assertEqual(len(object_calls), 2)
        self.assertEqual(object_calls[0][1]["headers"]["Accept"], "*/*")

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
