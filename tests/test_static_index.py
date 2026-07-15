from pathlib import Path
import unittest


class StaticIndexTests(unittest.TestCase):
    def _html(self) -> str:
        return (Path(__file__).resolve().parents[1] / "web_static" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_settings_menu_contains_glossary_link(self) -> None:
        html = self._html()

        self.assertIn('id="settingsButton"', html)
        self.assertIn('id="settingsDropdown"', html)
        self.assertIn('href="/glossary"', html)
        self.assertIn("热词库", html)
        self.assertIn("setSettingsOpen", html)

    def test_dashboard_uses_approved_peach_visual_system(self) -> None:
        html = self._html()

        self.assertIn("桃子播客工作台", html)
        self.assertIn('url("/assets/podcast-banner.png")', html)
        self.assertIn('src="/assets/peach-podcast-workbench.png"', html)
        self.assertIn("grid-template-rows: 108px 1fr", html)
        self.assertIn(".source.active::before", html)
        self.assertIn("avatarMarkup(sub, initial)", html)

    def test_dashboard_brand_assets_exist(self) -> None:
        asset_dir = Path(__file__).resolve().parents[1] / "web_static" / "assets"

        self.assertTrue((asset_dir / "podcast-banner.png").is_file())
        self.assertTrue((asset_dir / "peach-podcast-workbench.png").is_file())


if __name__ == "__main__":
    unittest.main()
