import secrets

from fastapi import HTTPException, Header
from typing import Optional

import config


def current_api_key() -> str:
    """当前生效的访问口令（环境变量 / 控制台「系统设置」热改后的值）。"""
    return str(getattr(config, "API_KEY", "") or "")


def validate_api_key(api_key_to_validate: str) -> bool:
    """校验调用本代理服务的 Bearer Token。"""
    expected = current_api_key()
    if not expected:
        return False
    return secrets.compare_digest(str(api_key_to_validate or ""), expected)


async def get_api_key(authorization: Optional[str] = Header(None)):
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="缺少 API Key，请在请求头中传入 Authorization: Bearer YOUR_API_KEY。",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="API Key 格式无效，请使用 Authorization: Bearer YOUR_API_KEY。",
        )

    api_key = authorization.replace("Bearer ", "", 1)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail="API Key 无效。")

    return api_key
