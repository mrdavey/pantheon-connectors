"""Host-managed portable transport; the native adapter retains all authority."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'connectors'))
from pantheon_connectors.broker.hermes_live import serve

if __name__ == '__main__':
    serve(Path(sys.argv[1]).resolve(), sys.stdin, sys.stdout)
