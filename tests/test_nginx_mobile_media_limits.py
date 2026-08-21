from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SITE_TEMPLATE = ROOT / "roles/nginx/templates/aurora-dashboard.nginx.j2"
ZONE_TEMPLATE = ROOT / "roles/nginx/templates/aurora-dashboard-rate-limits.conf.j2"


class NginxMobileMediaLimitTests(unittest.TestCase):
    def test_camera_media_has_a_separate_bounded_rate_limit(self) -> None:
        site = SITE_TEMPLATE.read_text(encoding="utf-8")
        zones = ZONE_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("zone=aurora_mobile_thumbnails:10m rate=300r/m", zones)
        media_location = site.index("location ^~ /mobile/v1/media/wxcam/thumb/")
        api_location = site.index("location ^~ /mobile/v1/", media_location + 1)
        media_block = site[media_location:api_location]

        self.assertIn("limit_req zone=aurora_mobile_thumbnails burst=60 nodelay;", media_block)
        self.assertIn("limit_conn aurora_per_ip 32;", media_block)
        self.assertIn(
            "proxy_pass http://{{ aurora_mobile_api_host }}:{{ aurora_mobile_api_port }}/media/wxcam/thumb/;",
            media_block,
        )

    def test_json_api_retains_its_stricter_limit(self) -> None:
        site = SITE_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("limit_req zone=aurora_mobile_api burst=20 nodelay;", site)
        self.assertIn("limit_conn aurora_per_ip 8;", site)
