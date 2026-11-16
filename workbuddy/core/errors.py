"""WorkBuddy 接入链路上的错误类型。

异常里只保留契约需要的字段，绝不保存上游响应体（可能带 token 或账号信息），
因此 ``str()`` / ``repr()`` 不会泄露内容。
"""

from __future__ import annotations


class WorkBuddyError(Exception):
    """本插件所有错误的基类。"""


_LIMIT_SCOPES = ("account", "ip", "model")


class UpstreamError(WorkBuddyError):
    """一次上游请求失败。

    ``status`` 是 HTTP 状态码（业务码非 0 时也为 200）；``code`` 是响应体里的
    业务码；``retry_after`` 是 429 的 ``Retry-After``（秒）；``limit_scope``
    只接受 ``account`` / ``ip`` / ``model``。
    """

    def __init__(self, status, code=None, retry_after=None, limit_scope=None):
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.limit_scope = limit_scope if limit_scope in _LIMIT_SCOPES else None
        parts = [f"upstream error (status={status}"]
        if code is not None:
            parts.append(f", code={code}")
        parts.append(")")
        super().__init__("".join(parts))


class ChatStreamError(WorkBuddyError):
    """上游聊天流为空、格式错误或缺少结束标记。"""

    def __init__(self, message, status=502, retry_after=None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(message)


class PoolEmptyError(WorkBuddyError):
    """账号池里没有可用账号。"""


class LoginError(WorkBuddyError):
    """交互式 OAuth 登录失败。"""


class LoginTimeoutError(LoginError):
    """轮询等待登录完成超时。"""


class LoginStateError(LoginError):
    """``state`` 不是当前登录流程签发的。"""


class TransferError(WorkBuddyError):
    """导出文件无法解析。"""
