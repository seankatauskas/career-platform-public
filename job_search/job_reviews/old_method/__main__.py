"""Portable screen entrypoint for local Codex sessions using a frozen input packet."""
import argparse
import json
from pathlib import Path
from .workspace import validate_packet, write_json
from .screen import screen, VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('screen',))
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    packet = json.loads(args.input.read_text())
    validate_packet(packet)
    write_json(args.output, {'screen_version': VERSION, 'items': screen(packet['jobs'])})


if __name__ == '__main__':
    main()
