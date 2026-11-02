"""Interactive decisions are terminal, including through restricted registries."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.autonomy import Autonomy
from goblin_core.tools import ApprovalRefused, ToolRegistry, object_schema


class ApprovalOwnershipTests(unittest.TestCase):
    def registry(self, target, decision):
        registry = ToolRegistry(approval="ask", approve=decision)
        registry.register("change", "Change a local file", object_schema({}),
                          lambda: target.write_text("changed"), risky=True)
        return registry

    def test_explicit_denial_never_reissues_the_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            deferred = []
            registry = self.registry(target, lambda name, arguments: False)
            registry.defer = lambda *arguments: deferred.append(arguments)
            for executor in [registry, registry.restricted(["change"])]:
                with self.assertRaises(ApprovalRefused) as failure:
                    executor.execute("change", {})
                self.assertTrue(failure.exception.denied)
                self.assertIn("No action ran and no approval is pending", str(failure.exception))
            self.assertFalse(target.exists())
            self.assertEqual(deferred, [])

    def test_accepted_operation_runs_once_without_a_second_request(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            deferred = []
            registry = self.registry(target, lambda name, arguments: True)
            registry.defer = lambda *arguments: deferred.append(arguments)
            registry.execute("change", {})
            self.assertEqual(target.read_text(), "changed")
            self.assertEqual(deferred, [])

    def test_broker_is_used_only_when_no_interactive_owner_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            requests = []
            registry = self.registry(target, None)
            def defer(name, arguments, run):
                requests.append(run)
                return {"approval": "requested"}
            registry.defer = defer
            self.assertEqual(registry.execute("change", {}), {"approval": "requested"})
            self.assertFalse(target.exists())
            self.assertEqual(len(requests), 1)
            requests[0]()
            self.assertEqual(target.read_text(), "changed")

    def test_denied_policy_never_defers(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            registry = self.registry(target, None)
            registry.approval = "deny_risky"
            requests = []
            registry.defer = lambda *arguments: requests.append(arguments)
            with self.assertRaises(ApprovalRefused):
                registry.execute("change", {})
            self.assertFalse(target.exists())
            self.assertEqual(requests, [])

    def test_policy_block_does_not_claim_an_approval_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            registry = self.registry(target, None)
            registry.approval = "deny_risky"
            with self.assertRaises(ApprovalRefused) as failure:
                registry.execute("change", {})
            message = str(failure.exception)
            self.assertFalse(failure.exception.denied)
            self.assertFalse(failure.exception.no_approver)
            self.assertIn("deny_risky policy does not allow change", message)
            self.assertIn("No action ran", message)
            self.assertIn("no approval is pending", message)
            self.assertNotIn("Approval required", message)
            self.assertFalse(target.exists())

    def test_missing_approval_path_is_not_reported_as_a_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            registry = self.registry(target, None)
            self.assertIsNone(registry.approve)
            self.assertIsNone(registry.defer)
            with self.assertRaises(ApprovalRefused) as failure:
                registry.execute("change", {})
            message = str(failure.exception)
            self.assertFalse(failure.exception.denied)
            self.assertTrue(failure.exception.no_approver)
            self.assertIn("no approval path", message)
            self.assertIn("No action ran", message)
            self.assertIn("no approval is pending", message)
            self.assertNotIn("operator denied", message)
            self.assertNotIn("Approval required", message)
            self.assertFalse(target.exists())


class AutonomyGateTests(unittest.TestCase):
    class Log:
        def append(self, *arguments):
            pass

    class Session:
        def __init__(self):
            self.log = AutonomyGateTests.Log()

    class Runtime:
        def __init__(self):
            self.session = AutonomyGateTests.Session()

    class Registry:
        def __init__(self, approval, allowed=None):
            self.approval = approval
            self.allowed = allowed
            self.approve = None

    def autonomy(self):
        instance = object.__new__(Autonomy)
        instance.request_approval = lambda *arguments: {"approval": "requested"}
        return instance

    def test_unleashed_runs_without_an_external_judge(self):
        result = Autonomy.gate(self.autonomy(), self.Runtime(), self.Registry("unleashed"),
                               "shell", {}, lambda: {"ran": True})
        self.assertEqual(result, {"ran": True})

    def test_ask_retains_the_operator_approval_path(self):
        result = Autonomy.gate(self.autonomy(), self.Runtime(), self.Registry("ask"),
                               "shell", {}, lambda: {"ran": True})
        self.assertEqual(result, {"approval": "requested"})

    def test_explicit_tool_grant_runs(self):
        result = Autonomy.gate(self.autonomy(), self.Runtime(),
                               self.Registry("ask", allowed=lambda name: True),
                               "shell", {}, lambda: {"ran": True})
        self.assertEqual(result, {"ran": True})
