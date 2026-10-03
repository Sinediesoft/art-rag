"""JWT（HS256）：七段權限控管的第 1 段「認證與授權」用的身分憑證（docs/adr/015）。

只用標準函式庫（hmac＋base64url），不另裝套件。只支援 HS256：header 的 alg 不是 HS256 一律拒絕，
擋掉 alg=none 這類偽造。驗證順序：格式 → alg → kid → 簽章（hmac.compare_digest）→ 效期 → 簽發者。

header 帶 kid（簽章金鑰的指紋）：後端重啟換了金鑰時，舊憑證回 TOKEN_STALE
（前端自動重新取得、不記錄），和「金鑰沒變但簽章不符」的竄改（TOKEN_INVALID，寫進拒絕並記錄）分開。
"""

import base64
import hashlib
import hmac
import json
import time

ALG = "HS256"


class TokenError(Exception):
    """憑證驗證失敗；code 是錯誤碼（shared/error_codes.md）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(signing_input: str, secret: str) -> str:
    return _b64e(hmac.new(secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest())


def key_id(secret: str) -> str:
    """簽章金鑰的指紋（不可逆推金鑰）：放在 header 的 kid。"""
    return hashlib.sha256(b"art-rag-kid:" + secret.encode()).hexdigest()[:12]


def encode(claims: dict, secret: str) -> str:
    header = {"alg": ALG, "typ": "JWT", "kid": key_id(secret)}
    head = _b64e(json.dumps(header, separators=(",", ":")).encode())
    body = _b64e(json.dumps(claims, ensure_ascii=False, separators=(",", ":")).encode())
    return f"{head}.{body}.{_sign(f'{head}.{body}', secret)}"


def peek(token: str) -> dict:
    """不驗簽章、只解出 payload（給前端示範竄改憑證、與錯誤訊息用）。"""
    try:
        return json.loads(_b64d(token.split(".")[1]))
    except (IndexError, ValueError):
        return {}


def decode(token: str, secret: str, issuer: str, now: float | None = None) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("TOKEN_INVALID", "身分憑證格式不對（不是 JWT）")
    head, body, sig = parts
    try:
        header = json.loads(_b64d(head))
        claims = json.loads(_b64d(body))
    except ValueError as e:
        raise TokenError("TOKEN_INVALID", "身分憑證無法解析") from e
    if header.get("alg") != ALG:
        raise TokenError("TOKEN_INVALID", f"不接受的簽章演算法：{header.get('alg')}（只收 {ALG}）")
    if header.get("kid") != key_id(secret):
        raise TokenError("TOKEN_STALE", "身分憑證是舊的簽章金鑰簽發的（後端重啟過），請重新取得")
    if not hmac.compare_digest(sig, _sign(f"{head}.{body}", secret)):
        raise TokenError("TOKEN_INVALID", "身分憑證的簽章不符（內容被改過或不是本系統簽發）")
    if not isinstance(claims.get("exp"), int | float) or (now or time.time()) >= claims["exp"]:
        raise TokenError("TOKEN_EXPIRED", "身分憑證已過期，請重新取得")
    if claims.get("iss") != issuer:
        raise TokenError("TOKEN_INVALID", "身分憑證不是本系統簽發")
    return claims
