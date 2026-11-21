"""站点解析器的协议级契约测试（无网络）。

这些解析器把站点返回的 JSON / HTML 文本翻译成本项目的用户数据契约。
它们是纯函数式的（不发起请求），直接喂真实结构的文本最省事。
"""

from __future__ import annotations

import json

import pytest
from app.adapters.sites.indexer.parser.hddolby import HDDolbySiteUserInfo
from app.adapters.sites.indexer.parser.mtorrent import MTorrentSiteUserInfo
from app.adapters.sites.indexer.parser.nexus_project import NexusProjectSiteUserInfo
from app.adapters.sites.indexer.parser.piggo import PiggoSiteUserInfo
from app.adapters.sites.indexer.parser.torrent_leech import TorrentLeechSiteUserInfo
from app.adapters.sites.indexer.spider import piggo as piggo_rules


def _mtorrent_profile() -> str:
    """M-Team 用户资料接口的合法响应。"""
    return json.dumps({
        "code": "0",
        "data": {
            "id": 42,
            "username": "tester",
            "roleName": "Power User",
            "memberStatus": {"createdDate": "2020-01-01 00:00:00"},
            "memberCount": {
                "uploaded": "1073741824",
                "downloaded": "536870912",
                "shareRate": 2.0,
                "bonus": 1000,
            },
        },
    })


class TestMTorrentParser:
    """M-Team 资料解析。"""

    def _parser(self) -> MTorrentSiteUserInfo:
        parser = MTorrentSiteUserInfo(
            site_name="M-Team",
            url="https://m-team.io/",
            site_cookie=None,
            apikey="key",
            token=None,
        )
        parser.userid = 42
        return parser

    @staticmethod
    def _offline(parser: MTorrentSiteUserInfo, body: str = "") -> None:
        """断开网络：解析器里所有 HTTP 取值都返回空串。"""
        parser._get_page_content = lambda *args, **kwargs: body  # type: ignore[method-assign]

    def test_parse_user_base_info(self) -> None:
        parser = self._parser()
        parser._parse_user_base_info(_mtorrent_profile())
        assert parser.userid == 42
        assert parser.username == "tester"
        assert parser.user_level == "Power User"
        assert parser.upload == 1073741824
        assert parser.download == 536870912
        assert parser.ratio == 2.0
        assert parser.bonus == 1000

    def test_parse_user_base_info_rejects_dict(self) -> None:
        with pytest.raises(TypeError):
            self._parser()._parse_user_base_info({"id": 1})  # type: ignore[arg-type]

    def test_parse_user_base_info_ignores_empty(self) -> None:
        parser = self._parser()
        parser._parse_user_base_info("")
        assert parser.username is None

    def test_parse_user_base_info_business_error(self) -> None:
        parser = self._parser()
        parser._parse_user_base_info(json.dumps({"code": "1", "message": "失败"}))
        assert parser.username is None

    def test_parse_user_torrent_seeding_info(self) -> None:
        parser = self._parser()
        self._offline(parser)
        payload = json.dumps({
            "code": "0",
            "data": {"data": [{"torrent": {"size": "1024", "source": "3"}}]},
        })
        parser._parse_user_torrent_seeding_info(payload)
        assert parser.seeding == 1
        assert parser.seeding_size == 1024

    def test_parse_user_torrent_seeding_info_bad_payload(self) -> None:
        parser = self._parser()
        self._offline(parser)
        parser._parse_user_torrent_seeding_info("{}")
        assert parser.seeding == 0

    def test_parse_message_unread_links(self) -> None:
        parser = self._parser()
        self._offline(parser)
        payload = json.dumps({
            "code": "0",
            "data": {"data": [{"unread": True, "title": "标题",
                               "createdDate": "2024-01-01", "context": "正文"}]},
        })
        assert parser._parse_message_unread_links(payload, []) is None
        assert parser.message_unread_contents


class TestHDDolbyParser:
    """HDDolby 资料解析（API 模式）。"""

    def _parser(self) -> HDDolbySiteUserInfo:
        return HDDolbySiteUserInfo(
            site_name="HDDolby",
            url="https://hddolby.com/",
            site_cookie=None,
            apikey="key",
            token=None,
        )

    def test_parse_user_base_info(self) -> None:
        parser = self._parser()
        payload = json.dumps({
            "status": 0,
            "data": [{
                "id": "7",
                "username": "tester",
                "class": "12",
                "uploaded": "1024",
                "downloaded": "512",
                "seedbonus": "10.5",
                "unread_messages": "3",
            }],
        })
        parser._parse_user_base_info(payload)
        assert parser.userid == "7"
        assert parser.username == "tester"
        assert parser.user_level == "Helper"
        assert parser.upload == 1024
        assert parser.download == 512
        assert parser.ratio == 2.0
        assert parser.bonus == 10.5
        assert parser.message_unread == 3

    def test_parse_user_base_info_rejects_dict(self) -> None:
        with pytest.raises(TypeError):
            self._parser()._parse_user_base_info({"id": 1})  # type: ignore[arg-type]

    def test_parse_user_base_info_error_status(self) -> None:
        parser = self._parser()
        parser._parse_user_base_info(json.dumps({"status": 1}))
        assert parser.userid is None

    def test_parse_user_torrent_seeding_info(self) -> None:
        parser = self._parser()
        payload = json.dumps({
            "status": 0,
            "data": [{"size": 2048, "seeders": 5}],
        })
        parser._parse_user_torrent_seeding_info(payload)
        assert parser.seeding == 1
        assert parser.seeding_size == 2048
        assert parser.seeding_info == [[5, 2048]]

    def test_parse_user_torrent_seeding_info_bad_payload(self) -> None:
        parser = self._parser()
        parser._parse_user_torrent_seeding_info(json.dumps({"status": 1}))
        assert parser.seeding == 0


class TestPiggoParser:
    """猪猪实验室资料解析。"""

    def _parser(self) -> PiggoSiteUserInfo:
        return PiggoSiteUserInfo(
            site_name="猪猪",
            url="https://piggo.me/",
            site_cookie=None,
            apikey="token",
            token=None,
        )

    def _profile(self) -> str:
        return json.dumps({
            "user": {
                "id": 9,
                "username": "tester",
                "enabled": True,
                "class": {"name": "VIP"},
                "stats": {"uploaded": "1 GB", "downloaded": "512 MB", "bonus": 42},
            }
        })

    def test_parse_user_base_info(self) -> None:
        parser = self._parser()
        parser._parse_user_base_info(self._profile())
        assert parser._profile["userid"] == 9
        assert parser._profile["username"] == "tester"
        assert parser._profile["user_level"] == "VIP"
        assert parser._profile["upload"] == 1024**3
        assert parser._profile["download"] == 512 * 1024**2
        assert parser._profile["bonus"] == 42

    def test_parse_user_base_info_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            self._parser()._parse_user_base_info({"user": {}})  # type: ignore[arg-type]

    def test_parse_user_base_info_missing_user(self) -> None:
        with pytest.raises(ValueError):
            self._parser()._parse_user_base_info(json.dumps({"data": {}}))

    def test_parse_user_base_info_disabled(self) -> None:
        payload = json.loads(self._profile())
        payload["user"]["enabled"] = False
        with pytest.raises(ValueError):
            self._parser()._parse_user_base_info(json.dumps(payload))

    def test_parse_user_base_info_missing_stats(self) -> None:
        payload = json.loads(self._profile())
        payload["user"].pop("stats")
        with pytest.raises(ValueError):
            self._parser()._parse_user_base_info(json.dumps(payload))


class TestPiggoRules:
    """猪猪 API 的信封与字段校验。"""

    def test_access_token(self) -> None:
        assert piggo_rules.access_token("key", "token") == "key"
        assert piggo_rules.access_token(None, "token") == "token"
        assert piggo_rules.access_token("", "") == ""

    def test_response_data_success(self) -> None:
        data = piggo_rules.response_data(200, {"code": "0", "data": {"x": 1}})
        assert data == {"x": 1}

    def test_response_data_notice(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            piggo_rules.response_data(200, {
                "code": "notice_unread",
                "data": {"notice_gate": {"title": "公告"}},
            })
        assert "公告" in str(excinfo.value)

    def test_response_data_auth_error(self) -> None:
        with pytest.raises(ValueError):
            piggo_rules.response_data(401, {"code": "401"}, "secret")

    def test_response_data_rate_limit(self) -> None:
        with pytest.raises(ValueError):
            piggo_rules.response_data(429, {"code": "429"})

    def test_response_data_invalid(self) -> None:
        with pytest.raises(ValueError):
            piggo_rules.response_data(200, None)

    def test_response_data_masks_token(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            piggo_rules.response_data(200, {"code": "1", "msg": "secret"}, "secret")
        assert "secret" not in str(excinfo.value)

    def test_integer(self) -> None:
        assert piggo_rules.integer("12", minimum=1) == 12
        assert piggo_rules.integer(12, minimum=1) == 12
        with pytest.raises(ValueError):
            piggo_rules.integer("0", minimum=1)
        with pytest.raises(ValueError):
            piggo_rules.integer("abc")
        with pytest.raises(ValueError):
            piggo_rules.integer(True)
        with pytest.raises(ValueError):
            piggo_rules.integer(1.5)

    def test_download_enclosure(self) -> None:
        enclosure = piggo_rules.download_enclosure("https://api.piggo.me/v1/api", 12, "token")
        assert enclosure is not None
        assert enclosure.startswith("[")
        assert enclosure.endswith("https://api.piggo.me/v1/api/torrents/12/download-link")
        assert piggo_rules.download_enclosure("https://api.piggo.me/v1/api", 12, "") == ""
        assert piggo_rules.download_enclosure("https://api.piggo.me/v1/api", 0, "token") == ""


class TestTorrentLeechParser:
    """TorrentLeech 资料页解析。"""

    def _parser(self) -> TorrentLeechSiteUserInfo:
        parser = TorrentLeechSiteUserInfo(
            site_name="TorrentLeech",
            url="https://www.torrentleech.org/",
            site_cookie=None,
            apikey=None,
            token=None,
        )
        parser.userid = "abc"
        return parser

    def test_parse_user_traffic_info(self) -> None:
        parser = self._parser()
        html = """
        <html><body>
          <div class="profile-username">tester</div>
          <div class="profile-uploaded"><span>1 GB</span></div>
          <div class="profile-downloaded"><span>512 MB</span></div>
          <div class="profile-ratio"><span>2.5</span></div>
          <table class="profileViewTable">
            <tr><td>Class</td><td>Uploader</td></tr>
            <tr><td>Registration date</td><td>2020-01-01</td></tr>
          </table>
          <span class="total-TL-points">1234</span>
        </body></html>
        """
        parser._parse_user_traffic_info(html)
        assert parser.username == "tester"
        assert parser.upload == 1024**3
        assert parser.download == 512 * 1024**2
        assert parser.ratio == 2.5
        assert parser.user_level == "Uploader"
        assert parser.join_at == "2020-01-01 00:00:00"
        assert parser.bonus == 1234

    def test_parse_user_traffic_info_empty_page(self) -> None:
        parser = self._parser()
        parser._parse_user_traffic_info("")
        assert parser.upload == 0

    def test_parse_site_page_without_userid(self) -> None:
        parser = TorrentLeechSiteUserInfo(
            site_name="TorrentLeech",
            url="https://www.torrentleech.org/",
            site_cookie=None,
            apikey=None,
            token=None,
        )
        parser._parse_site_page("<html><body>没有用户</body></html>")
        assert parser.err_msg == "未获取到用户ID"
        assert parser._user_detail_page is None

    def test_parse_site_page_with_userid(self) -> None:
        parser = TorrentLeechSiteUserInfo(
            site_name="TorrentLeech",
            url="https://www.torrentleech.org/",
            site_cookie=None,
            apikey=None,
            token=None,
        )
        parser._parse_site_page("<html><body><a href='/profile/abc/view'>me</a></body></html>")
        assert parser.userid == "abc"
        assert parser._user_detail_page == "profile/abc/view"
        assert parser._torrent_seeding_page == "profile/abc/seeding"


class TestNexusProjectParser:
    """NexusProject 变种站点的流量快速解析。"""

    def _parser(self) -> NexusProjectSiteUserInfo:
        return NexusProjectSiteUserInfo(
            site_name="NexusProject",
            url="https://example.com/",
            site_cookie=None,
            apikey=None,
            token=None,
        )

    def test_parse_user_traffic_info_from_span(self) -> None:
        parser = self._parser()
        html = """
        <html><body>
          <span id="user_info">上传：445.11 G 下载：29.61 G</span>
        </body></html>
        """
        parser._parse_user_traffic_info(html)
        assert parser.upload == round(445.11 * 1024**3)
        assert parser.download == round(29.61 * 1024**3)
        assert parser.ratio == round(445.11 / 29.61, 3)

    def test_parse_user_traffic_info_falls_back(self) -> None:
        """页面结构不认时回退到基类解析，不抛异常。"""
        parser = self._parser()
        parser._parse_user_traffic_info("<html><body>没有流量</body></html>")
        assert parser.upload >= 0
