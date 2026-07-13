from pathlib import Path
import unittest


class StaticIndexTests(unittest.TestCase):
    def test_settings_menu_contains_glossary_link(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "web_static" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="settingsButton"', html)
        self.assertIn('id="settingsDropdown"', html)
        self.assertIn('href="/glossary"', html)
        self.assertIn("热词库", html)
        self.assertIn("setSettingsOpen", html)


if __name__ == "__main__":
    unittest.main()
