"""密钥加密存储、匿名化与脱敏输出。"""
import hashlib
import hmac
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet

from .config import KEY_PATH


def _fernet() -> Fernet:
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
        try:
            KEY_PATH.chmod(0o600)
        except OSError:
            pass  # Windows 上可能不生效，不阻塞
    return Fernet(KEY_PATH.read_bytes())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def mask(key: str) -> str:
    """sk-abcdefgh1234 → sk-abc***1234，永不返回完整 Key。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}***{key[-4:]}"


def scrub(text: str, key: str = "") -> str:
    """从错误信息 / 响应摘要里抹掉完整 Key。"""
    if not text:
        return ""
    if key and key in text:
        text = text.replace(key, mask(key))
    return text


def redact_url(value: str) -> str:
    """保留定位所需的地址结构，移除用户信息、查询参数和片段。"""
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def anonymize_subject(subject_type: str, subject_id: str) -> str:
    """用途映射只保存不可逆、部署内稳定的匿名指纹。"""
    _fernet()
    material = f"usage-profile:v1\n{subject_type}\n{subject_id}".encode("utf-8")
    digest = hmac.new(KEY_PATH.read_bytes(), material, hashlib.sha256).hexdigest()
    return f"h1:{digest}"


def audit_hmac(purpose: str, value: str) -> str:
    """部署内审计 HMAC；purpose 防止不同用途的相同原文产生可关联指纹。"""
    _fernet()
    material = f"{purpose}:v1\n{value}".encode("utf-8")
    return hmac.new(KEY_PATH.read_bytes(), material, hashlib.sha256).hexdigest()


def credential_fingerprint(secret: str) -> str:
    return f"h1:{audit_hmac('credential', secret)}"
