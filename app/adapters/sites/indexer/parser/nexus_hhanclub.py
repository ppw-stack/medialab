# -*- coding: utf-8 -*-
"""
解析憨憨站点的用户资料、流量和做种信息。
"""

import re

from lxml import etree

from app.adapters.sites.indexer.compat import size as size_tools
from app.adapters.sites.indexer.compat import temporal as time_tools
from app.adapters.sites.indexer.compat import text as text_tools
from app.adapters.sites.indexer.compat.dom import DomUtils
from app.adapters.sites.indexer.parser import SiteSchema
from app.adapters.sites.indexer.parser.nexus_php import NexusPhpSiteUserInfo


class NexusHhanclubSiteUserInfo(NexusPhpSiteUserInfo):
    """
    解析 NexusHhanclub 站点的用户资料、流量与消息。
    """
    schema = SiteSchema.NexusHhanclub

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        解析站点的上传、下载及分享率信息。
        """
        super()._parse_user_traffic_info(html_text)

        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)

        try:
            # 上传、下载、分享率。页面结构变了就取不到文本——此时退回基类解析，
            # 不能让 IndexError 把整个用户数据刷新打挂。
            panel = html.xpath('//*[@id="user-info-panel"]')
            if not panel:
                return
            upload_text = panel[0].xpath('div[2]/div[2]/div[4]/text()')
            download_text = panel[0].xpath('div[2]/div[2]/div[5]/text()')
            ratio_text = panel[0].xpath('div[2]/div[1]/div[1]/div/text()')
            if not (upload_text and download_text and ratio_text):
                return
            upload_match = re.search(r"[_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)", upload_text[0])
            download_match = re.search(r"[_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)", download_text[0])
            ratio_match = re.search(r"分享率][:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+)", ratio_text[0])

            # 计算分享率
            self.upload = size_tools.parse_size(upload_match.group(1).strip()) if upload_match else 0
            self.download = size_tools.parse_size(download_match.group(1).strip()) if download_match else 0
            # 优先使用页面上的分享率
            calc_ratio = 0.0 if self.download <= 0.0 else round(self.upload / self.download, 3)
            self.ratio = text_tools.parse_float(ratio_match.group(1)) if (
                ratio_match and ratio_match.group(1).strip()) else calc_ratio
        finally:
            if html is not None:
                del html

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        解析用户额外信息，加入时间，等级。

        :param html_text: 当前页面或 API 响应文本
        """
        super()._parse_user_detail_info(html_text)

        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return
            # 加入时间
            join_at_text = html.xpath('//span[contains(text(), "加入日期")]/following-sibling::span/span/@title')
            if join_at_text:
                self.join_at = time_tools.normalize_datetime(join_at_text[0].strip())
        finally:
            if html is not None:
                del html

    def _get_user_level(self, html: etree._Element) -> None:
        """
        从用户等级标记中读取展示名称。
        """
        super()._get_user_level(html)
        user_level_path = html.xpath('//b[contains(@class, "_Name")]/text()')
        if user_level_path:
            self.user_level = user_level_path[0]
