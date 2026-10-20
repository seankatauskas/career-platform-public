import copy
from pathlib import Path
import runpy
import unittest

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check-instance-size-plan.py"))["check_plan"]
INSTANCE = "i-0fa677e47dee0134c"


def fixture():
    before = {"id": INSTANCE, "instance_type": "t3.large", "credit_specification": [{"cpu_credits": "standard"}], "root_block_device": [{"volume_id": "vol-original"}]}
    return {"resource_changes": [{"address": "aws_instance.host", "mode": "managed", "change": {"actions": ["update"], "before": before, "after": {**copy.deepcopy(before), "instance_type": "t3.medium"}, "after_unknown": {}}}]}


class InstanceSizePlanTests(unittest.TestCase):
    def test_resize_and_return(self):
        p = fixture(); CHECK(p, INSTANCE)
        c = p["resource_changes"][0]["change"]
        c["before"], c["after"] = c["after"], c["before"]
        CHECK(p, INSTANCE)

    def test_reject_replacement_and_other_changes(self):
        for key, value in [("actions", ["delete", "create"]), ("after_unknown", {"arn": True})]:
            p = fixture(); p["resource_changes"][0]["change"][key] = value
            with self.assertRaises(ValueError): CHECK(p, INSTANCE)
        for key, value in [("id", "i-12345678"), ("root_block_device", []), ("instance_type", "t3.small"), ("credit_specification", [{"cpu_credits": "unlimited"}])]:
            p = fixture(); p["resource_changes"][0]["change"]["after"][key] = value
            with self.assertRaises(ValueError): CHECK(p, INSTANCE)
        p = fixture(); p["resource_changes"].append({"address": "aws_iam_role.host", "mode": "managed", "change": {"actions": ["update"]}})
        with self.assertRaises(ValueError): CHECK(p, INSTANCE)

    def test_expected_stop_start_public_address_unknowns(self):
        p = fixture(); c = p["resource_changes"][0]["change"]
        c["before"].update(public_ip="192.0.2.1", public_dns="old")
        c["after_unknown"] = {"public_ip": True, "public_dns": True}
        CHECK(p, INSTANCE)
        c["after"]["public_ip"] = "192.0.2.2"
        with self.assertRaises(ValueError): CHECK(p, INSTANCE)

    def test_missing_or_failed(self):
        for p in ({}, {"resource_changes": []}, {"errored": True, **fixture()}):
            with self.assertRaises(ValueError): CHECK(p, INSTANCE)


if __name__ == "__main__": unittest.main()
