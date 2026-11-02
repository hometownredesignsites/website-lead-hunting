import unittest

from tools.site_audit import PageEvidenceParser, _age_fields, _copyright_footer_signal, _freshest_date


class SiteAuditTests(unittest.TestCase):
    def test_extracts_forms_privacy_blog_and_project_evidence(self):
        parser = PageEvidenceParser("https://example.test/")
        parser.feed("""
          <a href="/contact">Contact</a>
          <a href="/privacy-policy">Privacy Policy</a>
          <a href="/blog">Blog</a>
          <a href="/portfolio">Our Projects</a>
          <form action="/submit"><input name="email"></form>
          <article><a href="/blog/kitchen-update">Kitchen update</a>
            <time datetime="2025-03-04">March 4, 2025</time></article>
        """)
        self.assertEqual(parser.forms, 1)
        self.assertIn("https://example.test/privacy-policy", parser.privacy_links)
        self.assertIn("https://example.test/blog", parser.blog_links)
        self.assertIn("https://example.test/portfolio", parser.project_links)
        self.assertEqual(parser.date_candidates[0]["date"], "2025-03-04")
        self.assertEqual(parser.date_candidates[0]["source_url"], "https://example.test/blog/kitchen-update")

    def test_extracts_structured_content_dates(self):
        parser = PageEvidenceParser("https://example.test/blog/")
        parser.feed('''<script type="application/ld+json">
          {"@type":"BlogPosting","url":"/blog/new-kitchen","datePublished":"2026-02-01","dateModified":"2026-02-10"}
        </script>''')
        dates = {(row["source"], row["date"], row["source_url"]) for row in parser.date_candidates}
        self.assertIn(("jsonld:datePublished", "2026-02-01", "https://example.test/blog/new-kitchen"), dates)
        self.assertIn(("jsonld:dateModified", "2026-02-10", "https://example.test/blog/new-kitchen"), dates)

    def test_freshest_date_and_age_band_are_explicit(self):
        latest = _freshest_date([{"date": "2024-05-10"}, {"date": "2026-01-02"}])
        self.assertEqual(latest["date"], "2026-01-02")
        self.assertEqual(_age_fields("2000-01-01")["age_band"], "over_5_years")

    def test_current_copyright_year_is_not_freshness_evidence(self):
        signal = _copyright_footer_signal([2026], current_year=2026)
        self.assertEqual(signal["status"], "no_old_footer_year_detected")
        self.assertFalse(signal["counts_as_content_freshness"])
        self.assertEqual(signal["older_years"], [])


if __name__ == "__main__":
    unittest.main()
