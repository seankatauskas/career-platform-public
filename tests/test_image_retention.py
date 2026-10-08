"""Offline image-retention boundaries; Docker and ECR are fixture-backed."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from job_search import image_retention as retention


def digest(n):
    return f"sha256:{n:064x}"


def ref(n, repository="app"):
    return f"123456789012.dkr.ecr.us-east-2.amazonaws.com/career/{repository}@{digest(n)}"


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifests = {}
        self.images = []
        self.containers = []
        self.removed = []
        self.aws_calls = []
        self.unavailable = set()
        self.bad_inventory = False
        self.registry_error = False
        self.container_reads = 0
        self.start_during_cleanup = None
        self.current = self.release("current", 3, (1, 2, 3))
        self.previous = self.release("previous", 2, (4, 5, 6))
        self.old = self.release("old", 1, (7, 8, 9))

    def release(self, name, stamp, numbers):
        path = self.root / name
        path.mkdir()
        os.utime(path, (stamp, stamp))
        value = dict(zip(("app_image", "hermes_image", "reviewer_image"),
                         (ref(numbers[0]), ref(numbers[1], "hermes"), ref(numbers[2]))))
        self.manifests[path] = value
        for number, image_ref in zip(numbers, value.values()):
            self.images.append({"Id": digest(number), "RepoDigests": [image_ref], "RepoTags": []})
        return path

    def docker_run(self, argv, **kwargs):
        if argv[:3] == ["docker", "image", "ls"]:
            return "\n".join(image["Id"] for image in self.images)
        if argv[:3] == ["docker", "image", "inspect"]:
            self.assertEqual(argv[3:5], ["--format", retention.IMAGE_FORMAT])
            values = [image for image in self.images if image["Id"] in argv[5:]]
            if self.bad_inventory:
                values = values[:-1]
            return "\n".join(json.dumps(image) for image in values)
        if argv[:2] == ["docker", "ps"]:
            self.assertIn("--all", argv)
            self.container_reads += 1
            if self.container_reads > 1 and self.start_during_cleanup:
                self.containers = [self.start_during_cleanup]
            return "\n".join(f"{i:064x}" for i in range(1, len(self.containers) + 1))
        if argv[:3] == ["docker", "container", "inspect"]:
            return "\n".join(self.containers)
        if argv[:3] == ["docker", "image", "rm"]:
            self.assertEqual(argv[3], "--no-prune")
            self.assertEqual(len(argv), 5)
            self.removed.append(argv[4])
            return ""
        self.fail(f"unexpected command: {argv}")

    def aws(self, *args):
        self.aws_calls.append(args)
        self.assertEqual(args[:2], ("ecr", "batch-get-image"))
        if self.registry_error:
            raise OSError("registry unavailable")
        ids = json.loads(args[-1])
        self.assertLessEqual(len(ids), 100)
        return json.dumps({"images": [{"imageId": image} for image in ids
                                      if image["imageDigest"] not in self.unavailable]})

    def prune(self, **kwargs):
        return retention.prune({"aws_region": "us-east-2"}, current=self.current,
                               previous=kwargs.get("previous", self.previous),
                               read_manifest=self.manifests.__getitem__, run=self.docker_run, aws=self.aws)

    def test_removes_only_older_release_images_including_old_reviewer(self):
        result = self.prune()
        self.assertEqual(set(self.removed), {ref(7), ref(8, "hermes"), ref(9)})
        self.assertEqual(result["removed_refs"], 3)

    def test_containerd_digest_references_in_tags_are_eligible(self):
        for image in self.images:
            image["RepoTags"] = image["RepoDigests"][:]
        self.assertEqual(self.prune()["removed_refs"], 3)

    def test_running_and_stopped_container_images_are_preserved(self):
        self.containers = [digest(7), digest(8)]
        self.prune()
        self.assertEqual(self.removed, [ref(9)])

    def test_new_container_starting_after_inventory_is_preserved(self):
        self.start_during_cleanup = digest(7)
        self.prune()
        self.assertNotIn(ref(7), self.removed)

    def test_shared_identity_with_current_digest_is_preserved(self):
        self.images[6]["RepoDigests"].append(ref(1))
        self.prune()
        self.assertNotIn(ref(7), self.removed)

    def test_custom_tags_foreign_aliases_and_unknown_images_are_preserved(self):
        self.images[6]["RepoTags"] = ["local/qualification:keep"]
        self.images[7]["RepoDigests"].append("other.example/hermes@" + digest(8))
        self.images.append({"Id": digest(50), "RepoTags": [], "RepoDigests": [ref(50)]})
        self.prune()
        self.assertEqual(self.removed, [ref(9)])

    def test_staged_forward_release_images_are_preserved_even_on_rollback(self):
        self.release("staged", 4, (10, 11, 12))
        self.current, self.previous = self.previous, self.current
        self.prune()
        self.assertEqual(set(self.removed), {ref(7), ref(8, "hermes"), ref(9)})

    def test_missing_registry_images_are_not_removed(self):
        self.unavailable.add(digest(7))
        result = self.prune()
        self.assertNotIn(ref(7), self.removed)
        self.assertEqual(result["preserved_unavailable_images"], 1)

    def test_registry_error_prevents_all_removals(self):
        self.registry_error = True
        with self.assertRaises(OSError):
            self.prune()
        self.assertEqual(self.removed, [])

    def test_incomplete_inventory_prevents_all_removals(self):
        self.bad_inventory = True
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.prune()
        self.assertEqual(self.removed, [])

    def test_invalid_manifest_prevents_all_removals(self):
        (self.root / "broken").mkdir()
        with self.assertRaises(KeyError):
            self.prune()
        self.assertEqual(self.removed, [])

    def test_symlinked_release_is_never_a_cleanup_authority(self):
        (self.root / "linked").symlink_to(self.old)
        self.prune()
        self.assertEqual(len(self.removed), 3)

    def test_installer_staging_directory_does_not_block_cleanup(self):
        (self.root / ".install-in-progress").mkdir()
        self.assertEqual(self.prune()["removed_refs"], 3)

    def test_first_deployment_does_not_remove_images(self):
        self.assertEqual(self.prune(previous=None)["removed_refs"], 0)
        self.assertEqual(self.aws_calls, [])

    def test_registry_lookups_are_batched(self):
        refs = {ref(n) for n in range(201)}
        self.assertEqual(retention._available(refs, "us-east-2", self.aws), refs)
        self.assertEqual(len(self.aws_calls), 3)


if __name__ == "__main__":
    unittest.main()
