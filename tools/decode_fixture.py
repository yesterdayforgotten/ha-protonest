"""Inspect a captured Observe response body offline.

    .venv/bin/python tools/decode_fixture.py                    # resource summary
    .venv/bin/python tools/decode_fixture.py --device DEVICE_XXXXXXXXXXXXXXXX
    .venv/bin/python tools/decode_fixture.py --device DEVICE_... --label fan_control_settings

The input is the base64-decoded response body (a google.rpc.StreamBody).
Only CONFIRMED trait states are shown unless --accepted is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "protonest"))

from google.protobuf import json_format  # noqa: E402

from pynestproto import codec  # noqa: E402
from pynestproto.proto_gen.nestlabs.gateway import v2_pb2  # noqa: E402

DEFAULT_INPUT = ROOT / "old" / "sdk-deobfuscated" / "nest_proto"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--device", help="resource ID to dump traits for")
    parser.add_argument("--label", help="only this trait label (with --device)")
    parser.add_argument("--accepted", action="store_true", help="show ACCEPTED states too")
    args = parser.parse_args()

    codec.load_trait_types()
    frames = codec.parse_observe(args.input.read_bytes())
    states = [ts for f in frames for ts in f.traitStates]
    metas = {rm.resourceId: rm for f in frames for rm in f.resourceMetas}

    if not args.device:
        print(f"{len(frames)} frames, {len(metas)} resources, {len(states)} trait states\n")
        per_resource = Counter(ts.traitId.resourceId for ts in states)
        for rid, rm in metas.items():
            print(f"  {rid:28} {rm.type:50} {per_resource[rid]:4} states")
        unresolved = Counter(
            codec.trait_type_name(ts.patch.values)
            for ts in states
            if codec.unpack(ts.patch.values) is None
        )
        if unresolved:
            print("\nNot in schema:")
            for name, n in unresolved.most_common():
                print(f"  {n:3}  {name}")
        return

    for ts in states:
        if ts.traitId.resourceId != args.device:
            continue
        if args.label and ts.traitId.traitLabel != args.label:
            continue
        kinds = [v2_pb2.StateType.Name(s) for s in ts.stateTypes]
        if not args.accepted and "CONFIRMED" not in kinds:
            continue
        msg = codec.unpack(ts.patch.values)
        body = (
            json_format.MessageToDict(msg)
            if msg is not None
            else f"<not in schema: {codec.trait_type_name(ts.patch.values)}>"
        )
        print(f"{ts.traitId.traitLabel}  [{', '.join(kinds)}]  v{ts.monotonicVersion}")
        print(f"  {codec.trait_type_name(ts.patch.values)}")
        print("  " + json.dumps(body, indent=2).replace("\n", "\n  "))


if __name__ == "__main__":
    main()
