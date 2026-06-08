import unittest


class InstSciImportTests(unittest.TestCase):
    def test_public_config_import_path(self):
        from instsci.config import Config

        self.assertEqual(Config.__name__, "Config")

    def test_public_fetcher_and_model_import_paths(self):
        from instsci.fetcher import PaperFetcher
        from instsci.models import Paper

        self.assertEqual(PaperFetcher.__name__, "PaperFetcher")
        self.assertEqual(Paper.__name__, "Paper")
        self.assertEqual(PaperFetcher.__module__, "instsci.fetcher")
        self.assertEqual(Paper.__module__, "instsci.models")


if __name__ == "__main__":
    unittest.main()
