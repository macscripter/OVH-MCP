"""The small functions that decide what a symptom means."""

from simpl_ovh_mcp.kube.tools import _container_problems, _cpu_cores, _mem_gib, _rwx_capable
from simpl_ovh_mcp.simpl.diagnose import _last_meaningful_line, _render_log_line


def test_a_completed_init_container_is_not_a_problem():
    # Almost every healthy Simpl-Open pod has one of these.
    containers = [
        {"name": "copy-vault-env", "state": {"terminated": {"reason": "Completed", "exitCode": 0}}},
        {"name": "app", "state": {"running": {}}},
    ]
    assert _container_problems(containers) == []


def test_a_failed_init_container_is_reported_with_its_exit_code():
    containers = [{"name": "init", "state": {"terminated": {"reason": "Error", "exitCode": 1}}}]
    assert _container_problems(containers) == ["init: Error (exit 1)"]


def test_containers_that_are_merely_starting_are_ignored():
    containers = [{"name": "app", "state": {"waiting": {"reason": "ContainerCreating"}}}]
    assert _container_problems(containers) == []


def test_crash_loops_are_reported():
    containers = [{"name": "app", "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]
    assert _container_problems(containers) == ["app: CrashLoopBackOff"]


def test_cinder_is_not_rwx_and_nfs_is():
    assert not _rwx_capable("cinder.csi.openstack.org")
    assert not _rwx_capable("rancher.io/local-path")
    assert _rwx_capable("cluster.local/nfs-server-provisioner")
    assert _rwx_capable("nfs.csi.k8s.io")


def test_quantities_are_converted():
    assert _cpu_cores("500m") == 0.5
    assert _cpu_cores("8") == 8.0
    assert round(_mem_gib("32Gi"), 1) == 32.0
    assert round(_mem_gib("1048576Ki"), 1) == 1.0


def test_json_log_lines_are_reduced_to_their_message():
    line = '2026-09-18T18:24:47Z {"timestamp":"x","level":"ERROR","message":"Application run failed","logger":"org.springframework"}'
    assert _render_log_line(line) == "Application run failed"


def test_the_error_is_preferred_over_the_last_line():
    logs = "\n".join(
        [
            '{"level":"INFO","message":"starting"}',
            '{"level":"ERROR","message":"Connection to PostgreSQL refused"}',
            '{"level":"INFO","message":"shutting down"}',
        ]
    )
    assert "PostgreSQL" in _last_meaningful_line(logs)


def test_empty_logs_say_so():
    assert _last_meaningful_line("") == "(no log output)"


def test_nslookup_output_is_parsed_past_the_log_timestamps():
    """Pod logs are timestamped, and a timestamp is not an address.

    The resolver's own address line carries a port and must not be reported as the answer.
    """
    from simpl_ovh_mcp.simpl.platform import _addresses_from_nslookup

    logs = "\n".join(
        [
            "2026-09-19T01:53:13.798622789Z Server:\t\t10.3.0.10",
            "2026-09-19T01:53:13.798642106Z Address:\t10.3.0.10:53",
            "2026-09-19T01:53:13.798648949Z Non-authoritative answer:",
            "2026-09-19T01:53:13.798658368Z Name:\tauthority.fe.authority01.simpl-open-bridge.eu",
            "2026-09-19T01:53:13.798661523Z Address: 51.210.2.136",
        ]
    )
    assert _addresses_from_nslookup(logs) == ["51.210.2.136"]


def test_getent_output_is_parsed_too():
    from simpl_ovh_mcp.simpl.platform import _addresses_from_nslookup

    assert _addresses_from_nslookup("51.210.2.136   host.example.eu") == ["51.210.2.136"]
