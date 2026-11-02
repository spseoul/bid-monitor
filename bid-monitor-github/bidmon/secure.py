"""
부서별 비밀번호로 공고 데이터 잠그기 (AES-256-GCM)
  - 데이터 키(32바이트 난수) 하나로 payload 를 암호화하고,
    그 데이터 키를 부서별 비밀번호(PBKDF2-SHA256)로 각각 감싸(wrap) 둔다.
  - 비밀번호는 저장소에 절대 쓰지 않는다. PC 는 .env, GitHub Actions 는 Secrets 의
    ACCESS_PASSWORDS="건축기획:비번1;도시계획:비번2;도시환경설계:비번3;경영지원:비번4"
  - 브라우저(page.html)는 WebCrypto 로 같은 방식으로 푼다.
"""
import base64
import hashlib
import json
import os
import secrets
import zlib

ITER = 200_000
_b64 = lambda b: base64.b64encode(b).decode()
_unb64 = lambda s: base64.b64decode(s)


def passwords_from_env() -> dict[str, str]:
    raw = os.getenv("ACCESS_PASSWORDS", "").strip().strip('"')
    out = {}
    for part in raw.replace("\n", ";").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def _rsa():
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
    except ImportError as e:
        raise RuntimeError("암호화에 'cryptography' 패키지가 필요합니다:  py -m pip install cryptography") from e
    return serialization, hashes, rsa, padding


def gen_private_key_b64() -> str:
    """RSA-2048 개인키(PKCS8 DER)를 base64 한 줄로 — .env 의 ACCESS_PRIVATE_KEY 값"""
    serialization, hashes, rsa, padding = _rsa()
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _b64(k.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def private_key_from_env():
    raw = os.getenv("ACCESS_PRIVATE_KEY", "").strip().strip('"')
    if not raw:
        return None
    serialization, *_ = _rsa()
    return serialization.load_der_private_key(_unb64(raw), password=None)


def public_key_b64(priv) -> str:
    """브라우저(WebCrypto RSA-OAEP)가 쓸 공개키 — SPKI DER base64"""
    serialization, *_ = _rsa()
    return _b64(priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))


def depts_from_access_file(path, priv) -> dict[str, dict]:
    """관리자가 화면에서 등록한 부서 (bidmon/access.json: {부서: {salt, kek_enc}}) → {부서: {kek, salt}}"""
    if not priv or not os.path.exists(path):
        return {}
    serialization, hashes, rsa, padding = _rsa()
    out = {}
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return {}
    for dept, v in (data.get("depts") or {}).items():
        try:
            kek = priv.decrypt(_unb64(v["kek_enc"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
            out[dept] = {"kek": kek, "salt": _unb64(v["salt"])}
        except Exception:
            continue
    return out


def depts_from_passwords(passwords: dict[str, str]) -> dict[str, dict]:
    """.env 의 ACCESS_PASSWORDS → 같은 형식 (salt 는 비밀번호에서 결정적으로 → 매 빌드 동일)"""
    out = {}
    for dept, pw in passwords.items():
        salt = hashlib.sha256(("bidmon-access-salt|" + dept + ":" + pw).encode("utf-8")).digest()[:16]
        out[dept] = {"kek": derive(pw, salt), "salt": salt}
    return out


def _aes():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise RuntimeError("암호화에 'cryptography' 패키지가 필요합니다:  py -m pip install cryptography") from e
    return AESGCM


def derive(pw: str, salt: bytes, iters: int = ITER) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters, 32)


def rsa_seal(priv_or_pub, data: bytes) -> str:
    serialization, hashes, rsa, padding = _rsa()
    pub = priv_or_pub.public_key() if hasattr(priv_or_pub, "public_key") else priv_or_pub
    return _b64(pub.encrypt(data, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)))


def rsa_open(priv, b64: str) -> bytes:
    serialization, hashes, rsa, padding = _rsa()
    return priv.decrypt(_unb64(b64), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))


def state_key_from_previous(prev_access: dict | None, priv) -> bytes | None:
    """이전 배포의 access.state_key_enc(RSA 봉인) 에서 부서 상태 키를 복원 — 매 빌드 같은 키를 이어 써야 부서 관심공고·키워드가 유지됨"""
    if not prev_access or not priv or not prev_access.get("state_key_enc"):
        return None
    try:
        return rsa_open(priv, prev_access["state_key_enc"])
    except Exception:
        return None


def encrypt(payload: dict, depts: dict[str, dict], extra: dict | None = None,
            state_key: bytes | None = None, priv=None) -> tuple[dict, dict]:
    """depts: {부서: {"kek": 32바이트, "salt": 16바이트}}  (depts_from_passwords / depts_from_access_file 결과를 합친 것)
    → (meta 에 넣을 access 블록, data.json 에 쓸 암호문 블록)
    extra: 부서별로 함께 감싸 둘 작은 비밀값 (예: {"tok": GitHub 토큰} — 부서 상태 파일 저장용)"""
    AESGCM = _aes()
    dk = secrets.token_bytes(32)
    iv = secrets.token_bytes(12)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(dk).encrypt(iv, zlib.compress(raw, 6), None)
    keys = {}
    for dept, d in depts.items():
        kek = AESGCM(d["kek"])
        kiv = secrets.token_bytes(12)
        keys[dept] = {"salt": _b64(d["salt"]), "iv": _b64(kiv), "ct": _b64(kek.encrypt(kiv, dk, None))}
        if state_key:                                     # 부서 상태(관심공고·키워드) 파일용 고정 키 — 데이터 키와 달리 빌드마다 바뀌지 않음
            siv = secrets.token_bytes(12)
            keys[dept]["sk"] = {"iv": _b64(siv), "ct": _b64(kek.encrypt(siv, state_key, None))}
        for name, val in (extra or {}).items():
            if val:
                xiv = secrets.token_bytes(12)
                keys[dept][name] = {"iv": _b64(xiv), "ct": _b64(kek.encrypt(xiv, str(val).encode("utf-8"), None))}
    access = {"iter": ITER, "keys": keys}
    if state_key and priv:
        access["state_key_enc"] = rsa_seal(priv, state_key)   # 다음 빌드가 같은 상태 키를 이어 쓰도록 공개키로 봉인해 둠
    blob = {"enc": "aes-gcm-v1", "z": "deflate", "access": access, "iv": _b64(iv), "ct": _b64(ct)}
    return access, blob


def is_encrypted(obj) -> bool:
    return isinstance(obj, dict) and obj.get("enc") == "aes-gcm-v1"


def decrypt(blob: dict, access: dict | None, passwords: dict[str, str], depts: dict[str, dict] | None = None) -> dict:
    """어느 부서 키로든 풀리면 payload 반환 (passwords: .env 비밀번호, depts: access.json 에서 복원한 kek)"""
    AESGCM = _aes()
    access = access or blob.get("access") or {}
    iters = int(access.get("iter", ITER))
    cands = dict(depts or {})
    for dept, pw in passwords.items():
        k = access.get("keys", {}).get(dept)
        if k:
            salt = _unb64(k["salt"]) if k.get("salt") else _unb64(access.get("salt", ""))
            cands.setdefault(dept, {"kek": derive(pw, salt, iters), "salt": salt})
    last = None
    for dept, d in cands.items():
        k = access.get("keys", {}).get(dept)
        if not k:
            continue
        try:
            dk = AESGCM(d["kek"]).decrypt(_unb64(k["iv"]), _unb64(k["ct"]), None)
            pt = AESGCM(dk).decrypt(_unb64(blob["iv"]), _unb64(blob["ct"]), None)
            if blob.get("z") == "deflate":
                pt = zlib.decompress(pt)
            return json.loads(pt.decode("utf-8"))
        except Exception as e:
            last = e
    raise RuntimeError(f"데이터 잠금을 풀 수 없습니다 (ACCESS_PASSWORDS / ACCESS_PRIVATE_KEY 확인): {last}")
