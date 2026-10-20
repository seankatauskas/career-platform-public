#!/usr/bin/env python3
"""Allow only an in-place size change of the existing Standard production host."""
import argparse
import json
import re
import sys


def check_plan(plan, instance_id):
    if not re.fullmatch(r"i-[0-9a-f]{8,17}", instance_id):
        raise ValueError("invalid production instance ID")
    if plan.get("errored") or not isinstance(plan.get("resource_changes"), list):
        raise ValueError("missing or failed plan")
    found = False
    for resource in plan["resource_changes"]:
        change = resource["change"]
        actions = change["actions"]
        if resource["address"] == "aws_instance.host":
            found = True
            before, after = change["before"], change["after"]
            if actions not in (["update"], ["no-op"]):
                raise ValueError("instance creation or replacement forbidden")
            if before.get("id") != instance_id or after.get("id") != instance_id:
                raise ValueError("wrong instance")
            if any(v.get("instance_type") not in {"t3.medium", "t3.large"} for v in (before, after)):
                raise ValueError("unsupported size")
            if after.get("credit_specification") != [{"cpu_credits": "standard"}]:
                raise ValueError("Standard credits required")
            # Stop/start may reassign the auto-assigned public IPv4 and DNS.
            computed = {"public_ip", "public_dns"}
            unknowns = change.get("after_unknown", {})
            for key in computed:
                if before.get(key) != after.get(key) and not (unknowns.get(key) is True and after.get(key) is None):
                    raise ValueError("unexpected public address change")
            # Reject every other unknown, including storage/network configuration.
            def unknown(value):
                if isinstance(value, dict): return any(unknown(v) for v in value.values())
                if isinstance(value, list): return any(unknown(v) for v in value)
                return value is True
            if unknown({k: v for k, v in unknowns.items() if k not in computed}):
                raise ValueError("unknown post-resize attributes")
            if ({k: v for k, v in before.items() if k not in computed | {"instance_type"}} !=
                    {k: v for k, v in after.items() if k not in computed | {"instance_type"}}):
                raise ValueError("only instance_type may change")
        elif resource.get("mode") == "data" and actions in (["read"], ["no-op"]):
            continue
        elif actions != ["no-op"]:
            raise ValueError("unexpected dependency change: " + resource["address"])
    if not found:
        raise ValueError("production host missing")
    return {"scope": "instance-size", "instance_id": instance_id}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    print(json.dumps(check_plan(json.load(sys.stdin), args.instance_id)))
