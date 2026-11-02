# -*- coding: utf-8 -*-
"""天网 · 自建发现引擎后端包。

resource_finder 的 selfindex_pump / cloud_pump / ghcode_pump 三个泵背后真正干活的引擎。
完全不经过搜索引擎，从证书透明度日志 / Common Crawl / 互联网档案馆 / 云厂商测绘 /
GitHub 代码里挖资源站域名。

设计铁律（与 netforce 一致）：
· 纯标准库，零第三方依赖；
· 任何网络/解析异常一律返回 []（上游泵的 try/except 会把它当"零产出泵"自动降权）；
· 只产出域名字符串（无 scheme），交给泵统一拼 `https://` 走 consider() 判定链。
"""
