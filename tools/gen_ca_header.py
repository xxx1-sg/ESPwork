#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_ca_header.py —— 生成固件内的服务器 CA 链头文件 firmware/src/server_ca_pem.h

【为什么需要它】
ESP-IDF 内置的根证书包（esp_crt_bundle）里没有某些服务器用的根 CA，
实机上就会报：
    esp-x509-crt-bundle: No matching trusted root certificate found
    mbedtls_ssl_handshake returned -0x3000
本工具把「服务器实际下发的中间 CA + 其根 CA」抓下来、内嵌进固件，
让 HTTPS 仍然是**真实校验**的（不是把校验关掉）。
Cloudflare 隧道（trycloudflare）就是典型例子：证书由 Google Trust Services 的 WE1 签发。

做法：
  1. 连接目标 HTTPS 服务器，取出它下发的证书链；
  2. 用 cryptography 判断哪几张是 CA（中间证书）；
  3. 从 certifi 根证书库里找出签发链顶中间证书的那个根；
  4. 写出 server_ca_pem.h（PEM 字符串数组），并打印 SHA256 指纹便于核对。

用法（注意用带 cryptography 的解释器，即 PlatformIO 给 IDF 建的那个 venv）：
    D:\\ESP\\.platformio\\penv\\.espidf-6.1.0\\Scripts\\python.exe tools/gen_ca_header.py
    ... tools/gen_ca_header.py --host xxxx.trycloudflare.com --out firmware/src/server_ca_pem.h
"""

import argparse
import datetime
import hashlib
import os
import socket
import ssl
import sys

try:
    import cryptography.x509 as x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
except ImportError:
    print("缺少 cryptography：请用 D:\\ESP\\.platformio\\penv\\.espidf-6.1.0\\Scripts\\python.exe 运行")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def host_from_config() -> str | None:
    """从 app_config.h 里的 APP_SERVER_URL 解析主机名。"""
    path = os.path.join(ROOT, "firmware", "src", "app_config.h")
    if not os.path.isfile(path):
        return None
    import re
    text = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r'#define\s+APP_SERVER_URL\s+"https?://([^/"]+)/', text)
    return m.group(1) if m else None


def fetch_chain(host: str, port: int = 443) -> list[x509.Certificate]:
    """取服务器下发的证书链。

    注意：不同 Python 版本里 SSLSocket.get_unverified_chain() 返回的元素
    可能是 PEM 字节串，也可能是 DER 字节串，这里两种都兼容。
    """
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=20) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            raw = ss.get_unverified_chain()

    certs: list[x509.Certificate] = []
    if raw and isinstance(raw[0], bytes) and b"-----BEGIN CERTIFICATE-----" in raw[0]:
        blob = b"\n".join(raw)
        certs = x509.load_pem_x509_certificates(blob)
    else:
        for item in raw:
            der = item if isinstance(item, bytes) else item.public_bytes()
            certs.append(x509.load_der_x509_certificate(der))
    return certs


def is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def signed_by(child: x509.Certificate, parent: x509.Certificate) -> bool:
    """校验 child 是否由 parent 签发（按公钥类型选择算法）。"""
    try:
        pub = parent.public_key()
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(child.signature, child.tbs_certificate_bytes,
                       padding.PKCS1v15(), child.signature_hash_algorithm)
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(child.signature, child.tbs_certificate_bytes,
                       ec.ECDSA(child.signature_hash_algorithm))
        else:
            return False
        return True
    except Exception:
        return False


def certifi_roots() -> list[x509.Certificate]:
    try:
        import certifi
    except ImportError:
        print("提示: 没有 certifi，无法自动补根证书；若链里已含根证书则不影响。")
        return []
    return x509.load_pem_x509_certificates(open(certifi.where(), "rb").read())


def fp(cert: x509.Certificate) -> str:
    return ":".join("%02X" % b for b in cert.fingerprint(hashes.SHA256()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None, help="目标主机名（默认取 app_config.h 里的 APP_SERVER_URL）")
    ap.add_argument("--out", default=os.path.join(ROOT, "firmware", "src", "server_ca_pem.h"))
    ap.add_argument("--dump", default=None, help="把抓到的链同时存成 PEM 文件（便于人工核对）")
    a = ap.parse_args()

    host = a.host or host_from_config()
    if not host:
        print("无法确定主机名，请用 --host 指定")
        return 1
    print("目标服务器:", host)

    chain = fetch_chain(host)
    print("服务器下发 %d 张证书:" % len(chain))
    for i, c in enumerate(chain):
        print("  [%d] subject=%s" % (i, c.subject.rfc4514_string()))
        print("      issuer =%s  CA=%s" % (c.issuer.rfc4514_string(), is_ca(c)))
        print("      SHA256 =%s" % fp(c))

    leaf = chain[0]
    # 服务器一般只下发 叶子 + 中间CA（不含根），把中间 CA 全部保留
    trusted = [c for c in chain[1:] if is_ca(c)]
    if not trusted:
        print("!! 服务器没有下发中间 CA，无法仅靠链内证书完成校验（需另行获取中间证书）")
        return 2

    top = trusted[-1]
    self_signed = top.subject == top.issuer
    if self_signed:
        print("链内已包含自签根证书:", top.subject.rfc4514_string())
    else:
        print("链顶证书:", top.subject.rfc4514_string(),
              "由", top.issuer.rfc4514_string(), "签发（交叉签名链）")
        root = None
        for cand in certifi_roots():
            if cand.subject == top.issuer and signed_by(top, cand):
                root = cand
                break
        if root is not None:
            print("在本机根证书库里补到根 CA:", root.subject.rfc4514_string())
            print("          SHA256 =", fp(root))
            trusted.append(root)
        else:
            # 找不到自签根也没关系：服务器下发的中间 CA 本身就是可信锚点，
            # mbedtls 只要在 CA 列表里能找到签发链上任一张证书即可完成校验。
            print("提示: 本机根证书库里没找到对应的自签根 CA。")
            print("      直接使用服务器下发的中间 CA 作为信任锚（证书链仍会被真实校验）。")

    print("将内嵌 %d 张 CA 证书。" % len(trusted))

    # 生成头文件
    lines = [
        "/**",
        " * @file server_ca_pem.h",
        " * @brief 服务器 CA 证书链（自动生成，请勿手改）",
        " *",
        " * 由 tools/gen_ca_header.py 生成：",
        " *   目标主机 : %s" % host,
        " *   生成时间 : %s" % datetime.datetime.now().isoformat(timespec="seconds"),
        " *   包含证书 : %d 张（中间 CA + 根 CA，均为真实校验证书链的一部分）" % len(trusted),
    ]
    for c in trusted:
        lines.append(" *     - %s" % c.subject.rfc4514_string())
        lines.append(" *       SHA256 %s" % fp(c))
    lines += [
        " *",
        " * 当服务器证书的根 CA 不在 ESP-IDF 内置根证书包里时（例如 Cloudflare 隧道",
        " * 用的 Google Trust Services），把本文件里的链配给 esp_http_client 的 cert_pem，",
        " * 就能在**保持真实证书校验**的前提下连上。",
        " */",
        "#pragma once",
        "",
        "static const char SERVER_CA_PEM[] =",
    ]
    for c in trusted:
        pem = c.public_bytes(serialization.Encoding.PEM).decode("ascii")
        for line in pem.rstrip("\n").split("\n"):
            lines.append('    "%s\\n"' % line)
    lines.append("    ;")
    lines.append("")
    lines.append("// 链上叶子证书（仅作说明，不参与固件校验逻辑）")
    lines.append("// leaf subject: %s" % leaf.subject.rfc4514_string())
    lines.append("")

    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print("已生成:", a.out, "(%d 字节)" % os.path.getsize(a.out))

    if a.dump:
        with open(a.dump, "wb") as f:
            for c in trusted:
                f.write(c.public_bytes(serialization.Encoding.PEM))
        print("已导出证书链 PEM:", a.dump)
    return 0


if __name__ == "__main__":
    sys.exit(main())