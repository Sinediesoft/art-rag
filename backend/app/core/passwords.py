"""帳號密碼登入的密碼雜湊（docs/adr/045）：scrypt，只用標準函式庫，和 jwt.py 一樣不另裝套件。

存成一行文字 `scrypt$n$r$p$鹽$雜湊`（鹽與雜湊是 base64url），放在 .env 的 ACCOUNT_PASSWORD_HASHES，
不進 git。產生雜湊：

    cd backend && uv run python -m app.core.passwords
"""

import base64
import getpass
import hashlib
import hmac
import os

# scrypt 參數：N=2^14、r=8、p=1（約 16 MB 記憶體、數十毫秒），登入才算一次，讓離線暴力猜很慢
N, R, P = 2**14, 8, 1
KEY_LEN = 32


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=KEY_LEN, maxmem=64 * 1024 * 1024
    )


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    return f"scrypt${N}${R}${P}${_b64e(salt)}${_b64e(_derive(password, salt, N, R, P))}"


def verify_password(password: str, encoded: str) -> bool:
    """格式不對一律當成不符（不丟例外）：.env 寫錯的帳號就是登入不了，不會讓登入端點 500。"""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        got = _derive(password, _b64d(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, _b64d(digest))
    except (ValueError, TypeError, MemoryError):
        return False


# 帳號不存在或沒設密碼時也算一次 scrypt：回應時間不會透露哪些帳號能登入
DUMMY_HASH = hash_password("art-rag-dummy", salt=b"\0" * 16)


def main() -> None:
    first = getpass.getpass("密碼：")
    if first != getpass.getpass("再輸入一次："):
        raise SystemExit("兩次輸入的密碼不一樣")
    if len(first) < 8:
        raise SystemExit("密碼至少要 8 個字元")
    print(hash_password(first))


if __name__ == "__main__":
    main()
