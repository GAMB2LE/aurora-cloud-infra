"""Recovery endpoints must not confuse transport, host identity and login."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/edge_tunnel_server"
SPEC = importlib.util.spec_from_file_location("edge_status", ROLE / "files/aurora-edge-access-status.py")
STATUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATUS)
WIRE_KEY = b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes(range(32))
KEY = "ssh-ed25519 " + base64.b64encode(WIRE_KEY).decode()
FINGERPRINT = "SHA256:" + base64.b64encode(hashlib.sha256(WIRE_KEY).digest()).decode().rstrip("=")
SCAN = "[127.0.0.1]:2201 " + KEY + "\n"
DEFAULTS = yaml.safe_load((ROOT / "inventory/group_vars/aurora_cloud.yml").read_text())


def environment():
    env = Environment(loader=FileSystemLoader(ROLE / "templates"), undefined=StrictUndefined,
                      keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True)
    env.filters["to_json"] = json.dumps
    return env


class StatusTests(unittest.TestCase):
    def endpoint(self, **changes):
        return dict(name="ass-linux", kind="ssh", port=2201,
                    expected_fingerprint=FINGERPRINT, **changes)

    def run_scan(self, output=SCAN, expected=FINGERPRINT):
        with mock.patch.object(STATUS.socket, "create_connection"), mock.patch.object(
            STATUS.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")
        ) as scan:
            endpoint = self.endpoint()
            endpoint["expected_fingerprint"] = expected
            result = STATUS.probe_endpoint(endpoint)
        return result, scan

    def test_exact_host_key_does_not_claim_authentication(self):
        result, scan = self.run_scan()
        self.assertEqual(result["identity"], "verified")
        self.assertEqual(result["transport"], "ssh_handshake")
        self.assertEqual(result["authentication"], "not_tested")
        self.assertEqual(scan.call_args.args[0][-1], "127.0.0.1")
        self.assertNotIn("ssh", scan.call_args.args[0])

    def test_mismatch_missing_trust_and_nonssh_listener_fail_closed(self):
        result, _ = self.run_scan(expected="SHA256:" + "A" * 43)
        self.assertEqual(result["identity"], "mismatch")
        result, _ = self.run_scan(expected="")
        self.assertEqual(result["identity"], "unconfigured")
        result, _ = self.run_scan(output="HTTP/1.1 200 OK\n")
        self.assertEqual(result["identity"], "probe_failed")
        result, _ = self.run_scan(output="[127.0.0.1]:2201 ssh-ed25519 YWJj\n")
        self.assertEqual(result["identity"], "probe_failed")

    def test_unreachable_port_never_runs_keyscan(self):
        with mock.patch.object(STATUS.socket, "create_connection", side_effect=ConnectionRefusedError), \
                mock.patch.object(STATUS.subprocess, "run") as scan:
            result = STATUS.probe_endpoint(self.endpoint())
        self.assertEqual(result["transport"], "unreachable")
        scan.assert_not_called()

    def test_keyscan_timeout_is_bounded_and_not_a_healthy_listener(self):
        with mock.patch.object(STATUS.socket, "create_connection"), mock.patch.object(
            STATUS.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh-keyscan", 5)
        ):
            result = STATUS.probe_endpoint(self.endpoint())
        self.assertEqual(result["identity"], "probe_failed")

    def test_reverse_listener_closes_if_nx_target_is_down(self):
        with mock.patch.object(STATUS.socket, "create_connection") as connect:
            connect.return_value.__enter__.return_value.recv.return_value = b""
            result = STATUS.probe_endpoint(dict(name="ass-nx", kind="nx", port=2401))
        self.assertEqual(result["transport"], "closed_after_connect")
        self.assertEqual(result["desktop"], "not_tested")

    def test_silent_nx_listener_is_not_reported_as_desktop_success(self):
        with mock.patch.object(STATUS.socket, "create_connection") as connect:
            connect.return_value.__enter__.return_value.recv.side_effect = socket.timeout
            result = STATUS.probe_endpoint(dict(name="ass-nx", kind="nx", port=2401))
        self.assertEqual(result["transport"], "tcp_open_no_greeting")
        self.assertEqual(result["authentication"], "not_tested")

    def test_nx_failure_does_not_hide_working_ssh(self):
        endpoints = DEFAULTS["edge_tunnel_server_status_endpoints"]
        def probe(endpoint, timeout):
            return dict(endpoint, identity="verified", transport="closed_after_connect")
        with mock.patch.object(STATUS, "probe_endpoint", side_effect=probe):
            result = STATUS.report(endpoints, 3)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["ssh_transport_status"], "verified")
        self.assertEqual(result["nx_tcp_status"], "degraded")

    def test_status_config_cannot_become_an_arbitrary_network_probe(self):
        config = dict(version=1, endpoints=[dict(x) for x in DEFAULTS["edge_tunnel_server_status_endpoints"]])
        self.assertEqual(len(STATUS.validate_config(config)), 6)
        config["endpoints"][0]["port"] = 22
        with self.assertRaises(ValueError):
            STATUS.validate_config(config)


class PolicyTests(unittest.TestCase):
    def test_ansible_preflight_rejects_wildcard_listeners_and_option_overrides(self):
        ansible = ROOT / ".venv/bin/ansible-playbook"
        if not ansible.exists():
            self.skipTest("local Ansible environment is not installed")
        preflight = yaml.safe_load((ROLE / "tasks/main.yml").read_text())[:5]
        values = {key: value for key, value in DEFAULTS.items() if key.startswith("edge_tunnel_server_")}
        values.update(edge_tunnel_server_enabled=True, edge_tunnel_server_authorized_keys=[
            dict(name="ass-linux-digitalocean", key=KEY + " migration-test", listen_ports=["127.0.0.1:2201"])
        ])
        with tempfile.TemporaryDirectory() as temp:
            playbook = Path(temp) / "preflight.yml"
            def run():
                playbook.write_text(yaml.safe_dump([dict(hosts="aurora-cloud", gather_facts=False,
                                                       vars=values, tasks=preflight)]))
                return subprocess.run([str(ansible), "-i", "aurora-cloud,", "-c", "local", str(playbook)],
                                      capture_output=True, text=True)
            valid = run()
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            values["edge_tunnel_server_authorized_keys"][0]["listen_ports"] = ["0.0.0.0:2201"]
            self.assertNotEqual(run().returncode, 0)
            values["edge_tunnel_server_authorized_keys"][0]["listen_ports"] = ["127.0.0.1:2201"]
            values["edge_tunnel_server_authorized_keys"][0]["key_options"] = ["permitopen=any"]
            self.assertNotEqual(run().returncode, 0)

    def test_per_key_listener_is_not_expanded_to_other_hosts(self):
        values = dict(DEFAULTS, edge_tunnel_server_authorized_keys=[
            dict(name="ass-linux-digitalocean", key=KEY, listen_ports=["127.0.0.1:2201", "127.0.0.1:2401"]),
            dict(name="aps-proxmox-digitalocean", key=KEY, listen_ports=["127.0.0.1:2212"]),
        ])
        lines = environment().get_template("authorized-keys.j2").render(values).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('permitlisten="127.0.0.1:2201",permitlisten="127.0.0.1:2401"', lines[0])
        self.assertNotIn("2212", lines[0])
        self.assertNotIn("2201", lines[1])
        self.assertTrue(all(line.startswith("restrict,port-forwarding,") for line in lines))

    def test_both_named_clouds_are_opt_in_and_source_sync_stays_unchanged(self):
        play = yaml.safe_load((ROOT / "playbooks/edge_tunnel_server.yml").read_text())[0]
        self.assertEqual(play["hosts"], "aurora-cloud:aurora-cloud-droplet")
        self.assertFalse(DEFAULTS["edge_tunnel_server_enabled"])
        self.assertFalse(DEFAULTS["edge_tunnel_server_replace_authorized_keys"])
        self.assertFalse(DEFAULTS["edge_tunnel_server_status_timer_enabled"])
        self.assertFalse(DEFAULTS["edge_source_sync_use_reverse_tunnels"])

    def test_config_has_exact_endpoint_trust_and_unknown_is_explicit(self):
        values = dict(DEFAULTS, edge_tunnel_server_expected_host_fingerprints={"ass-linux": FINGERPRINT})
        config = json.loads(environment().get_template("edge-access-status.json.j2").render(values))
        endpoints = STATUS.validate_config(config)
        self.assertEqual(endpoints[0]["expected_fingerprint"], FINGERPRINT)
        self.assertEqual(endpoints[1]["expected_fingerprint"], "")

    def test_restricted_match_policy_does_not_leak_to_another_user(self):
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        if not Path(sshd).exists() or not shutil.which("ssh-keygen"):
            self.skipTest("OpenSSH server/client not installed")
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(temp / "host")], check=True)
            fragment = temp / "tunnel.conf"
            fragment.write_text(environment().get_template("edge-tunnel-sshd.conf.j2").render(DEFAULTS))
            config = temp / "sshd_config"
            config.write_text(f"HostKey {temp / 'host'}\nInclude {fragment}\nPasswordAuthentication no\n"
                              "UsePAM yes\nSubsystem sftp internal-sftp\n")
            command = [sshd, "-T", "-f", str(config), "-C"]
            tunnel = subprocess.run(command + ["user=aurora-tunnel,host=localhost,addr=127.0.0.1"], capture_output=True, text=True)
            if "Missing privilege separation directory" in tunnel.stderr:
                self.skipTest("runner has no OpenSSH privilege separation directory")
            self.assertEqual(tunnel.returncode, 0, tunnel.stderr)
            policy = dict(line.split(" ", 1) for line in tunnel.stdout.splitlines())
            expected = dict(maxsessions="0", allowtcpforwarding="remote", permitopen="none", gatewayports="no",
                            allowstreamlocalforwarding="no", permittty="no", authenticationmethods="publickey",
                            clientaliveinterval="30", clientalivecountmax="3")
            for name, value in expected.items():
                self.assertEqual(policy[name], value)
            self.assertEqual(policy["permitlisten"].split(), DEFAULTS["edge_tunnel_server_listen_ports"])
            operator = subprocess.run(command + ["user=existing-operator,host=localhost,addr=127.0.0.1"], capture_output=True, text=True, check=True)
            self.assertIn("maxsessions 10\n", operator.stdout)
            self.assertIn("allowtcpforwarding yes\n", operator.stdout)
            self.assertIn("passwordauthentication no\n", operator.stdout)

    def test_validation_failure_has_a_backup_restore_and_handler_rechecks(self):
        tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        policy = next(task for task in tasks if "rescue" in task)
        self.assertTrue(any("edge_tunnel_server_config.backup_file" in str(task) for task in policy["rescue"]))
        self.assertEqual(policy["rescue"][-1]["ansible.builtin.fail"]["msg"].count("not reloaded"), 1)
        handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
        self.assertIn("reload_sshd.yml", handlers[0]["ansible.builtin.include_tasks"])
        reload = yaml.safe_load((ROLE / "tasks/reload_sshd.yml").read_text())
        self.assertEqual(reload[0]["ansible.builtin.command"], "/usr/sbin/sshd -t")
        self.assertEqual(reload[1]["ansible.builtin.service"]["state"], "reloaded")
        activation = next(index for index, task in enumerate(tasks)
                          if task.get("ansible.builtin.include_tasks") == "reload_sshd.yml")
        first_key_change = next(index for index, task in enumerate(tasks) if "ansible.builtin.blockinfile" in task)
        self.assertLess(activation, first_key_change)


if __name__ == "__main__":
    unittest.main()
