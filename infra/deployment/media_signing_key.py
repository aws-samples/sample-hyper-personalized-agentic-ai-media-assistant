"""Create the CloudFront signing key for the /media/* signed cookies (run once).

    .venv/bin/python infra/deployment/media_signing_key.py --config infra/environments/<name>.json

Generates an RSA-2048 key pair with openssl (in memory, never written to disk),
registers the public key with CloudFront, stores {"keyPairId", "privateKey"} in
the Secrets Manager secret <prefix>/media-signing-key (read by GET
/media/session), and writes the public key id to the SSM parameter
/<prefix>/media-signing/key-pair-id (read by the frontend stack's key group).

Does nothing when the secret already exists. --rotate makes a new key; deploy
the frontend phase afterwards so the key group trusts it, then delete the old
CloudFront public key it prints.
"""
import argparse
import json
import os
import subprocess
import time

from config import DEFAULT_CONFIG, load_config
from deploy import aws_environment


def generate():
    private = subprocess.run(["openssl", "genrsa", "-traditional", "2048"], check=True,
                             capture_output=True, text=True).stdout
    public = subprocess.run(["openssl", "rsa", "-pubout"], input=private, check=True,
                            capture_output=True, text=True).stdout
    assert "BEGIN RSA PRIVATE KEY" in private and "BEGIN PUBLIC KEY" in public
    return private, public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--rotate", action="store_true", help="Replace an existing key")
    args = parser.parse_args()
    cfg = load_config(args.config)
    os.environ.clear()
    os.environ.update(aws_environment(cfg["region"]))
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    assert session.client("sts").get_caller_identity()["Account"] == cfg["account"]
    secrets, ssm, cloudfront = session.client("secretsmanager"), session.client("ssm"), session.client("cloudfront")
    secret_name = f'{cfg["prefix"]}/media-signing-key'
    param_name = f'/{cfg["prefix"]}/media-signing/key-pair-id'

    old = None
    try:
        old = json.loads(secrets.get_secret_value(SecretId=secret_name)["SecretString"])["keyPairId"]
    except secrets.exceptions.ResourceNotFoundException:
        pass
    if old and not args.rotate:
        print("Signing key already exists:", old)
        return

    private, public = generate()
    stamp = time.strftime("%Y%m%d%H%M%S")
    key_id = cloudfront.create_public_key(PublicKeyConfig={
        "CallerReference": f'{cfg["prefix"]}-media-{stamp}', "Name": f'{cfg["prefix"]}-media-{stamp}',
        "EncodedKey": public, "Comment": f'{cfg["prefix"]} /media/* signed cookies'})["PublicKey"]["Id"]
    value = json.dumps({"keyPairId": key_id, "privateKey": private})
    if old:
        secrets.put_secret_value(SecretId=secret_name, SecretString=value)
    else:
        secrets.create_secret(Name=secret_name, SecretString=value,
            Description="CloudFront signing key for the /media/* signed cookies",
            Tags=[{"Key": k, "Value": v} for k, v in cfg["tags"].items()])
    ssm.put_parameter(Name=param_name, Value=key_id, Type="String", Overwrite=True)
    print("CloudFront public key:", key_id)
    print("Secret:", secret_name, "| SSM parameter:", param_name)
    if old:
        print("Deploy the frontend phase, then delete the old CloudFront public key:", old)


if __name__ == "__main__":
    main()
