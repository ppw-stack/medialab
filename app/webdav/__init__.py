"""WebDAV 适配层。

**只用 wsgidav 的协议层**（PROPFIND / GET / HEAD 的标准实现），认证、限流、
审计走本项目自己的 ASGI 中间件（:mod:`app.webdav.guard`）。

为什么这么切：wsgidav 经 a2wsgi 跑在**线程池**里，而本项目的 DB 是
``AsyncEngine``（aiosqlite）。在 WSGI 线程里回主 loop 查库会崩（实测
``RecursionError``）。故让 wsgidav **完全不碰 DB**，只做文件系统操作；
身份通过 ASGI scope header 传递。
"""
