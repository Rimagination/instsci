import unittest

from instsci.sources import elsevier_api


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


if __name__ == "__main__":
    unittest.main()
