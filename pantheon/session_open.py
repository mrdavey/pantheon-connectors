import pathlib,sys
p=pathlib.Path(__file__).resolve().parent
sys.path[:0]=[str(p/'vendor'),str(p/'connectors')]
from pantheon_connectors.codex_reopen import main
main()
