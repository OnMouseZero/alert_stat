import argparse
import ipaddress
import subprocess
from pathlib import Path


def safe_unlink(path_obj):
    if path_obj.exists():
        path_obj.unlink()


def main():
    parser = argparse.ArgumentParser(description="使用 openssl 生成 self-signed 证书")
    parser.add_argument(
        "--host",
        default="localhost",
        help="证书主机名或地址，多个用英文逗号分隔，例如 10.0.0.179,172.21.8.102",
    )
    parser.add_argument("--output-dir", default="certs", help="证书输出目录，默认 certs")
    parser.add_argument("--name", default="alert_dashboard", help="证书文件名前缀，默认 alert_dashboard")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    key_path = output_dir / f"{args.name}.key"
    csr_path = output_dir / f"{args.name}.csr"
    crt_path = output_dir / f"{args.name}.crt"
    san_path = output_dir / f"{args.name}.cnf"

    host_items = [item.strip() for item in args.host.split(",") if item.strip()]
    primary_host = host_items[0] if host_items else "localhost"
    san_lines = []
    dns_index = 1
    ip_index = 1
    for host_item in host_items:
        try:
            ipaddress.ip_address(host_item)
            san_lines.append(f"IP.{ip_index} = {host_item}")
            ip_index += 1
        except ValueError:
            san_lines.append(f"DNS.{dns_index} = {host_item}")
            dns_index += 1

    san_path.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name=req_distinguished_name",
                "x509_extensions=v3_req",
                "prompt=no",
                "[req_distinguished_name]",
                f"CN={primary_host}",
                "[v3_req]",
                "basicConstraints = CA:FALSE",
                "keyUsage = critical, digitalSignature, keyEncipherment",
                "extendedKeyUsage = serverAuth",
                f"subjectAltName = @alt_names",
                "[alt_names]",
                *san_lines,
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
