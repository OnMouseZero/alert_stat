import argparse
import ipaddress
import subprocess
from pathlib import Path


def safe_unlink(path_obj):
    if path_obj.exists():
        path_obj.unlink()


def main():
    parser = argparse.ArgumentParser(description="使用 openssl 生成 self-signed 证书")
    parser.add_argument("--host", default="localhost", help="证书主机名，默认 localhost")
    parser.add_argument("--output-dir", default="certs", help="证书输出目录，默认 certs")
    parser.add_argument("--name", default="alert_dashboard", help="证书文件名前缀，默认 alert_dashboard")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    key_path = output_dir / f"{args.name}.key"
    csr_path = output_dir / f"{args.name}.csr"
    crt_path = output_dir / f"{args.name}.crt"
    san_path = output_dir / f"{args.name}.cnf"

    try:
        ipaddress.ip_address(args.host)
        san_entry = f"IP.1 = {args.host}"
    except ValueError:
        san_entry = f"DNS.1 = {args.host}"

    san_path.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name=req_distinguished_name",
                "x509_extensions=v3_req",
                "prompt=no",
                "[req_distinguished_name]",
                f"CN={args.host}",
                "[v3_req]",
                "basicConstraints = CA:FALSE",
                "keyUsage = critical, digitalSignature, keyEncipherment",
                "extendedKeyUsage = serverAuth",
                f"subjectAltName = @alt_names",
                "[alt_names]",
                san_entry,
            ]
        ),
        encoding="utf-8",
    )

    cmd_key = [
        "openssl",
        "genrsa",
        "-out",
        str(key_path),
        "2048",
    ]
    cmd_csr = [
        "openssl",
        "req",
        "-new",
        "-key",
        str(key_path),
        "-out",
        str(csr_path),
        "-config",
        str(san_path),
    ]
    cmd_crt = [
        "openssl",
        "x509",
        "-req",
        "-days",
        "3650",
        "-in",
        str(csr_path),
        "-signkey",
        str(key_path),
        "-out",
        str(crt_path),
        "-extensions",
        "v3_req",
        "-extfile",
        str(san_path),
    ]

    subprocess.run(cmd_key, check=True)
    subprocess.run(cmd_csr, check=True)
    subprocess.run(cmd_crt, check=True)

    safe_unlink(csr_path)
    safe_unlink(san_path)

    print(f"证书已生成: {crt_path}")
    print(f"私钥已生成: {key_path}")


if __name__ == "__main__":
    main()
