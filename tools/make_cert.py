"""生成本地 HTTPS 用的自签名证书（可选，1.6）：手机经局域网访问时，浏览器只允许 HTTPS 页面安装 PWA / 注册 Service Worker。
用法：  pip install cryptography
        python tools/make_cert.py [--host 192.168.1.20 --host my-pc.local]
输出：  data/certs/cert.pem、data/certs/key.pem；然后在 data/user_config.yaml 里写：
        server:
          host: 0.0.0.0
          token: "你的访问口令"
          ssl_certfile: data/certs/cert.pem
          ssl_keyfile: data/certs/key.pem
注意：自签名证书需要在手机上手动信任（iOS：把 cert.pem 通过 AirDrop / 邮件发到手机 → 安装描述文件 → 设置 > 通用 > 关于本机 > 证书信任设置 里打开完全信任）。
更省事的替代：mkcert（自动建立本机信任的 CA）或经 Tailscale 等私网访问。"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def main():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        sys.exit("需要先安装：pip install cryptography")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", default=[], help="额外的域名或 IP（可多次）")
    ap.add_argument("--days", type=int, default=825)
    a = ap.parse_args()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = ["localhost", socket.gethostname()] + [h for h in a.host if not h.replace(".", "").isdigit()]
    ips = ["127.0.0.1"] + local_ips() + [h for h in a.host if h.replace(".", "").isdigit()]
    san = [x509.DNSName(n) for n in dict.fromkeys(names)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in dict.fromkeys(ips)]
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "stock-app local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=a.days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    out = ROOT / "data" / "certs"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (out / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    print(f"已生成 {out / 'cert.pem'}、{out / 'key.pem'}\n证书包含：{[n for n in names]} / {ips}")


if __name__ == "__main__":
    main()
