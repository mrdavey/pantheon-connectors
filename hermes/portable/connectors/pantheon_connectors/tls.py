"""Use native OS trust without disabling certificate or hostname verification."""
import os
from pathlib import Path
import ssl
import sys


def client_context():
    context = ssl.create_default_context()
    # python.org macOS builds can have no installed OpenSSL CA bundle. Use the
    # system-maintained trust bundle, while respecting explicit CA overrides.
    if (sys.platform == 'darwin' and not os.environ.get('SSL_CERT_FILE')
            and not os.environ.get('SSL_CERT_DIR') and Path('/etc/ssl/cert.pem').is_file()):
        context.load_verify_locations(cafile='/etc/ssl/cert.pem')
    return context
