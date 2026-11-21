"""Verified TLS from the control plane to an AWS source database.

Listing a source's tables connected to MySQL in plaintext and to PostgreSQL
with libpq's default sslmode=prefer, which takes any certificate or none, with
the source's password on the wire. Both now verify the server certificate
against Amazon RDS's CA bundle and check that it names the endpoint.

rds-ca-bundle.pem is Amazon RDS's global bundle
(https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem). It is not
in git: the control-plane image downloads it at build time and checks its
SHA-256 (backend/Dockerfile.release); `make ca-bundle` does the same locally.
"""
import os
import ssl

RDS_CA_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rds-ca-bundle.pem')


def mysql_tls(ca_file=RDS_CA_BUNDLE):
    """pymysql.connect() keyword arguments for verified TLS."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return {'ssl': ctx}


def postgresql_tls(ca_file=RDS_CA_BUNDLE):
    """psycopg2.connect() keyword arguments for verified TLS."""
    return {'sslmode': 'verify-full', 'sslrootcert': ca_file}
