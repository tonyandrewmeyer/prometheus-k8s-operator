# Copyright 2020 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import json
import logging
import socket
import uuid
from unittest.mock import patch

import pytest
import yaml
from helpers import cli_arg
from scenario import ActiveStatus, BlockedStatus, Container, Exec, MaintenanceStatus, Relation, State

from charm import PROMETHEUS_CONFIG, PrometheusCharm

logger = logging.getLogger(__name__)

RELATION_NAME = "metrics-endpoint"
DEFAULT_JOBS = [{"metrics_path": "/metrics"}]
SCRAPE_METADATA = {
    "model": "provider-model",
    "model_uuid": str(uuid.uuid4()),
    "application": "provider",
    "charm_name": "provider-charm",
}


@pytest.fixture(autouse=True)
def mock_pvc_capacity():
    with patch.object(PrometheusCharm, "_get_pvc_capacity", return_value="1Gi"):
        yield


def _container():
    """Create a prometheus container suitable for config tests."""
    return Container(
        "prometheus",
        can_connect=True,
        execs={Exec(["update-ca-certificates", "--fresh"], return_code=0, stdout="")},
    )


class TestCharm:
    def test_grafana_is_provided_port_and_source(self, context):
        rel = Relation("grafana-source")
        container = _container()
        state_in = State(containers=[container], relations=[rel])
        state_out = context.run(context.on.relation_joined(rel), state_in)

        fqdn = socket.getfqdn()
        local_unit_data = state_out.get_relation(rel.id).local_unit_data
        assert local_unit_data["grafana_source_host"] == f"http://{fqdn}:9090"

    def test_default_cli_log_level_is_info(self, context):
        container = _container()
        state_in = State(containers=[container])
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--log.level") == "info"

    def test_invalid_log_level_defaults_to_debug(self, context, caplog):
        container = _container()
        state_in = State(containers=[container], config={"log_level": "bad-level"})
        with caplog.at_level(logging.WARNING):
            state_out = context.run(context.on.config_changed(), state_in)
            assert "Invalid loglevel: bad-level given" in caplog.text

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--log.level") == "debug"

    def test_valid_log_level_is_accepted(self, context):
        container = _container()
        state_in = State(containers=[container], config={"log_level": "warn"})
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--log.level") == "warn"

    def test_ingress_relation_not_set(self, context):
        container = _container()
        state_in = State(containers=[container], leader=True)
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        fqdn = socket.getfqdn()
        assert cli_arg(plan, "--web.external-url") == f"http://{fqdn}:9090"

    def test_ingress_relation_set(self, context):
        ingress_rel = Relation(
            "ingress",
            remote_app_data={
                "ingress": yaml.safe_dump(
                    {"prometheus-k8s/0": {"url": "http://test:80"}}
                )
            },
        )
        container = _container()
        state_in = State(
            containers=[container], relations=[ingress_rel], leader=True,
        )
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--web.external-url") == "http://test:80"

    def test_web_external_has_no_effect(self, context):
        container = _container()
        state_in = State(
            containers=[container],
            config={"web_external_url": "http://test:80/sub/path"},
            leader=True,
        )
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        fqdn = socket.getfqdn()
        assert cli_arg(plan, "--web.external-url") == f"http://{fqdn}:9090"

    def test_metrics_wal_compression_is_not_enabled_by_default(self, context):
        container = _container()
        state_in = State(containers=[container])
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.wal-compression") is None

    def test_metrics_wal_compression_can_be_enabled(self, context):
        container = _container()
        state_in = State(containers=[container], config={"metrics_wal_compression": True})
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert (
            cli_arg(plan, "--storage.tsdb.wal-compression")
            == "--storage.tsdb.wal-compression"
        )

    @pytest.mark.parametrize("unit", ["y", "w", "d", "h", "m", "s"])
    def test_valid_metrics_retention_times_can_be_set(self, context, unit):
        retention_time = f"1{unit}"
        container = _container()
        state_in = State(
            containers=[container],
            config={"metrics_retention_time": retention_time},
        )
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.time") == retention_time

    @pytest.mark.parametrize("retention_time", ["1x", "5m1y2d"])
    def test_invalid_metrics_retention_times_can_not_be_set(self, context, retention_time):
        container = _container()
        state_in = State(
            containers=[container],
            config={"metrics_retention_time": retention_time},
        )
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.time") is None

    @pytest.mark.parametrize("unit", ["y", "w", "d", "h", "m", "s"])
    def test_global_evaluation_interval_can_be_set(self, context, unit):
        eval_int = f"1{unit}"
        container = _container()
        state_in = State(containers=[container], config={"evaluation_interval": eval_int})
        state_out = context.run(context.on.config_changed(), state_in)

        config_path = (
            state_out.get_container("prometheus").get_filesystem(context)
            / PROMETHEUS_CONFIG.lstrip("/")
        )
        gconfig = yaml.safe_load(config_path.read_text())["global"]
        assert gconfig["evaluation_interval"] == eval_int

    def test_default_scrape_config_is_always_set(self, context):
        container = _container()
        state_in = State(containers=[container])
        state_out = context.run(context.on.config_changed(), state_in)

        config_path = (
            state_out.get_container("prometheus").get_filesystem(context)
            / PROMETHEUS_CONFIG.lstrip("/")
        )
        config = yaml.safe_load(config_path.read_text())
        prometheus_job = None
        for job in config["scrape_configs"]:
            if job["job_name"] == "prometheus":
                prometheus_job = job
        assert prometheus_job is not None, "No default config found"

    def test_honor_labels_is_always_set_in_scrape_configs(self, context):
        rel = Relation(
            RELATION_NAME,
            remote_app_data={
                "scrape_metadata": json.dumps(SCRAPE_METADATA),
                "scrape_jobs": json.dumps(DEFAULT_JOBS),
            },
        )
        container = _container()
        state_in = State(containers=[container], relations=[rel])
        state_out = context.run(context.on.config_changed(), state_in)

        config_path = (
            state_out.get_container("prometheus").get_filesystem(context)
            / PROMETHEUS_CONFIG.lstrip("/")
        )
        config = yaml.safe_load(config_path.read_text())
        for job in config["scrape_configs"]:
            if job["job_name"] != "prometheus":
                assert "honor_labels" in job
                assert job["honor_labels"] is True

    def test_configuration_reload(self, context):
        # Step 1: establish baseline pebble plan
        container = _container()
        state1 = State(containers=[container])
        state_mid = context.run(context.on.config_changed(), state1)

        # Step 2: change evaluation_interval (config file only, not command args)
        state2 = dataclasses.replace(state_mid, config={"evaluation_interval": "1234m"})
        with patch("prometheus_client.Prometheus.reload_configuration") as reload_mock:
            context.run(context.on.config_changed(), state2)
            reload_mock.assert_called()

    def test_configuration_reload_success(self, context):
        container = _container()
        state1 = State(containers=[container])
        state_mid = context.run(context.on.config_changed(), state1)

        state2 = dataclasses.replace(state_mid, config={"evaluation_interval": "1234m"})
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.config_changed(), state2)
        assert isinstance(state_out.unit_status, ActiveStatus)

    def test_configuration_reload_error(self, context):
        container = _container()
        state1 = State(containers=[container])
        state_mid = context.run(context.on.config_changed(), state1)

        state2 = dataclasses.replace(state_mid, config={"evaluation_interval": "1234m"})
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=False
        ):
            state_out = context.run(context.on.config_changed(), state2)
        assert isinstance(state_out.unit_status, BlockedStatus)

    def test_configuration_reload_read_timeout(self, context):
        container = _container()
        state1 = State(containers=[container])
        state_mid = context.run(context.on.config_changed(), state1)

        state2 = dataclasses.replace(state_mid, config={"evaluation_interval": "1234m"})
        with patch(
            "prometheus_client.Prometheus.reload_configuration",
            return_value="read_timeout",
        ):
            state_out = context.run(context.on.config_changed(), state2)
        assert isinstance(state_out.unit_status, MaintenanceStatus)


def alerting_config(config):
    config_yaml = config[1]
    config_dict = yaml.safe_load(config_yaml)
    return config_dict.get("alerting")


def global_config(config_yaml):
    config_dict = yaml.safe_load(config_yaml)
    return config_dict["global"]


def scrape_config(config_yaml, job_name):
    config_dict = yaml.safe_load(config_yaml)
    scrape_configs = config_dict["scrape_configs"]
    for config in scrape_configs:
        if config["job_name"] == job_name:
            return config
    return None


REMOTE_SCRAPE_METADATA = {
    "model": "remote-model",
    "model_uuid": "be44e4b8-32eb-48e1-a843-b1c12e47b9b3",
    "application": "remote-app",
    "charm_name": "remote-charm",
}

LABELED_ALERT_RULES = {
    "groups": [
        {
            "name": "ZZZ_a5edc336-b02e-4fad-b847-c530500c1c86_consumer-tester_alerts",
            "rules": [
                {
                    "alert": "CPUOverUse",
                    "expr": "process_cpu_seconds_total > 0.12",
                    "labels": {
                        "severity": "Low",
                        "juju_model": "ZZZ-model",
                        "juju_model_uuid": "a5edc336-b02e-4fad-b847-c530500c1c86",
                        "juju_application": "zzz-app",
                    },
                },
            ],
        },
        {
            "name": "AAA_a5edc336-b02e-4fad-b847-c530500c1c86_consumer-tester_alerts",
            "rules": [
                {
                    "alert": "PrometheusTargetMissing",
                    "expr": "up == 0",
                    "labels": {
                        "severity": "critical",
                        "juju_model": "AAA-model",
                        "juju_model_uuid": "a5edc336-b02e-4fad-b847-c530500c1c86",
                        "juju_application": "aaa-app",
                    },
                },
            ],
        },
    ]
}

UNLABELED_ALERT_RULES = {
    "groups": [
        {
            "name": "ZZZ_group_alerts",
            "rules": [
                {
                    "alert": "CPUOverUse",
                    "expr": "process_cpu_seconds_total > 0.12",
                    "labels": {"severity": "Low"},
                },
            ],
        },
        {
            "name": "AAA_group_alerts",
            "rules": [
                {
                    "alert": "PrometheusTargetMissing",
                    "expr": "up == 0",
                    "labels": {"severity": "critical"},
                },
            ],
        },
    ]
}


class TestAlertsFilename:
    def test_charm_writes_meaningful_alerts_filename_1(self, context):
        # WHEN relation data includes both scrape_metadata and labeled alerts
        rel = Relation(
            RELATION_NAME,
            remote_app_data={
                "scrape_metadata": json.dumps(REMOTE_SCRAPE_METADATA),
                "alert_rules": json.dumps(LABELED_ALERT_RULES),
            },
        )
        container = _container()
        state_in = State(containers=[container], relations=[rel])
        state_out = context.run(context.on.config_changed(), state_in)

        # THEN rules filename is derived from the contents of alert labels
        fs = state_out.get_container("prometheus").get_filesystem(context)
        rules_dir = fs / "etc/prometheus/rules"
        files = {f"/etc/prometheus/rules/{f.name}" for f in rules_dir.iterdir()}
        assert files == {
            f"/etc/prometheus/rules/juju_ZZZ-model_a5edc336_zzz-app_{RELATION_NAME}_{rel.id}.rules"
        }

    def test_charm_writes_meaningful_alerts_filename_2(self, context):
        # TODO: merge the contents of these tests into a single test (and fix the bug!)
        # WHEN relation data includes only labeled alerts (no scrape_metadata)
        rel = Relation(
            RELATION_NAME,
            remote_app_data={
                "alert_rules": json.dumps(LABELED_ALERT_RULES),
            },
        )
        container = _container()
        state_in = State(containers=[container], relations=[rel])
        state_out = context.run(context.on.config_changed(), state_in)

        # THEN rules filename is derived from the first (!) rule's topology labels
        # TODO derive filename from _sorted_ rules so it's deterministic?
        fs = state_out.get_container("prometheus").get_filesystem(context)
        rules_dir = fs / "etc/prometheus/rules"
        files = {f"/etc/prometheus/rules/{f.name}" for f in rules_dir.iterdir()}
        assert files == {
            f"/etc/prometheus/rules/juju_ZZZ-model_a5edc336_zzz-app_{RELATION_NAME}_{rel.id}.rules"
        }

    def test_charm_writes_meaningful_alerts_filename_3(self, context):
        # WHEN relation data includes scrape_metadata but _unlabeled_ alerts
        rel = Relation(
            RELATION_NAME,
            remote_app_data={
                "scrape_metadata": json.dumps(REMOTE_SCRAPE_METADATA),
                "alert_rules": json.dumps(UNLABELED_ALERT_RULES),
            },
        )
        container = _container()
        state_in = State(containers=[container], relations=[rel])
        state_out = context.run(context.on.config_changed(), state_in)

        # THEN rules filename is derived from the contents of scrape_metadata
        fs = state_out.get_container("prometheus").get_filesystem(context)
        rules_dir = fs / "etc/prometheus/rules"
        files = {f"/etc/prometheus/rules/{f.name}" for f in rules_dir.iterdir()}
        assert files == {
            f"/etc/prometheus/rules/juju_remote-model_be44e4b8_remote-app_{RELATION_NAME}_{rel.id}.rules"
        }

    def test_charm_writes_meaningful_alerts_filename_4(self, context):
        # TODO: merge the contents of these tests into a single test (and fix the bug!)
        # WHEN relation data includes only _unlabeled_ alerts (no scrape_metadata)
        rel = Relation(
            RELATION_NAME,
            remote_app_data={
                "alert_rules": json.dumps(UNLABELED_ALERT_RULES),
            },
        )
        container = _container()
        state_in = State(containers=[container], relations=[rel])
        state_out = context.run(context.on.config_changed(), state_in)

        # THEN rules filename is derived from the first (!) rule's group name
        # TODO derive filename from _sorted_ rules so it's deterministic?
        fs = state_out.get_container("prometheus").get_filesystem(context)
        rules_dir = fs / "etc/prometheus/rules"
        files = {f"/etc/prometheus/rules/{f.name}" for f in rules_dir.iterdir()}
        assert files == {
            f"/etc/prometheus/rules/juju_ZZZ_group_alerts_{RELATION_NAME}_{rel.id}.rules"
        }


class TestPebblePlan:
    """Test the pebble plan is kept up-to-date (situational awareness)."""

    def test_no_restart_nor_reload_when_nothing_changes(self, context):
        """When nothing changes, calling `_configure()` shouldn't result in downtime."""
        # GIVEN a pebble plan (established via first config-changed)
        container = _container()
        state1 = State(containers=[container])
        state_mid = context.run(context.on.config_changed(), state1)

        initial_plan = state_mid.get_container("prometheus").plan

        # WHEN config_changed fires again without any changes
        # Note: scenario doesn't persist container filesystem across ctx.run() calls,
        # so we mock config/alerts generation to report "no change" (as would happen
        # in a real charm when the config and alerts haven't changed).
        with patch("prometheus_client.Prometheus.reload_configuration") as reload_mock, \
             patch.object(PrometheusCharm, "_generate_prometheus_config", return_value=False), \
             patch.object(PrometheusCharm, "_set_alerts", return_value=False):
            state_out = context.run(context.on.config_changed(), state_mid)

        # THEN pebble service is unchanged
        assert (
            state_out.get_container("prometheus").plan.to_dict()
            == initial_plan.to_dict()
        )

        # AND reload is not invoked
        reload_mock.assert_not_called()

    def test_workload_hot_reloads_when_some_config_options_change(self, context):
        """Some config options go into the config file and require a reload (not restart)."""
        # GIVEN a pebble plan
        container = _container()
        state1 = State(containers=[container])
        state_mid = context.run(context.on.config_changed(), state1)

        initial_plan = state_mid.get_container("prometheus").plan

        # WHEN evaluation_interval is changed
        state2 = dataclasses.replace(state_mid, config={"evaluation_interval": "1234s"})
        with patch("prometheus_client.Prometheus.reload_configuration") as reload_mock:
            state_out = context.run(context.on.config_changed(), state2)

        # THEN a reload is invoked
        reload_mock.assert_called()

        # BUT pebble service is unchanged
        assert (
            state_out.get_container("prometheus").plan.to_dict()
            == initial_plan.to_dict()
        )


class TestConfigMaximumRetentionSize:
    """Test the charmcraft.yaml option 'maximum_retention_size'."""

    def test_default_maximum_retention_size_is_80_percent(self, context):
        """This test is here to guarantee backwards compatibility.

        Since charmcraft.yaml provides a default (which forms a contract), we need to prevent
        changing it unintentionally.
        """
        # GIVEN a capacity limit in binary notation (k8s notation) via mock_pvc_capacity
        # AND the maximum_retention_size config is left unspecified (default)
        container = _container()
        state_in = State(containers=[container])
        state_out = context.run(context.on.config_changed(), state_in)

        # THEN the pebble plan has the adjusted capacity of 80%
        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.size") == "0.8GB"

        # AND WHEN the config option is set to 50% and then back to default
        state2 = dataclasses.replace(state_out, config={"maximum_retention_size": "50%"})
        state_out2 = context.run(context.on.config_changed(), state2)

        # Then back to default (80%)
        state3 = dataclasses.replace(state_out2, config={})
        state_out3 = context.run(context.on.config_changed(), state3)

        # THEN the pebble plan is back to 80%
        plan = state_out3.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.size") == "0.8GB"

    @pytest.mark.parametrize(
        "set_point, read_back",
        [("0%", "0GB"), ("50%", "0.5GB"), ("100%", "1GB")],
    )
    def test_multiplication_factor_applied_to_pvc_capacity(
        self, context, set_point, read_back
    ):
        """The `--storage.tsdb.retention.size` arg must be multiplied by maximum_retention_size."""
        container = _container()
        state_in = State(
            containers=[container], config={"maximum_retention_size": set_point}
        )
        state_out = context.run(context.on.config_changed(), state_in)

        plan = state_out.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.size") == read_back

    def test_invalid_retention_size_config_option_string(self, context):
        container = _container()

        # GIVEN a running charm with default values
        state_in = State(containers=[container])
        state_out = context.run(context.on.config_changed(), state_in)
        assert isinstance(state_out.unit_status, ActiveStatus)

        # WHEN the config option is set to an invalid string
        state2 = dataclasses.replace(state_out, config={"maximum_retention_size": "42"})
        state_out2 = context.run(context.on.config_changed(), state2)

        # THEN cli arg is unspecified and the unit is blocked
        plan = state_out2.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.size") is None
        assert isinstance(state_out2.unit_status, BlockedStatus)

        # AND WHEN the config option is set to another invalid string
        state3 = dataclasses.replace(
            state_out2, config={"maximum_retention_size": "4GiB"}
        )
        state_out3 = context.run(context.on.config_changed(), state3)

        # THEN cli arg is unspecified and the unit is blocked
        plan = state_out3.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.size") is None
        assert isinstance(state_out3.unit_status, BlockedStatus)

        # AND WHEN the config option is corrected
        state4 = dataclasses.replace(
            state_out3, config={"maximum_retention_size": "42%"}
        )
        state_out4 = context.run(context.on.config_changed(), state4)

        # THEN cli arg is updated and the unit goes back to active
        plan = state_out4.get_container("prometheus").plan
        assert cli_arg(plan, "--storage.tsdb.retention.size") == "0.42GB"
        assert isinstance(state_out4.unit_status, ActiveStatus)


class TestTlsConfig:
    def _make_state(self, scrape_jobs):
        """Build a State with a metrics-endpoint relation carrying the given scrape_jobs."""
        rel = Relation(
            RELATION_NAME,
            remote_app_data={
                "scrape_jobs": json.dumps(scrape_jobs),
            },
            remote_units_data={
                0: {
                    "prometheus_scrape_unit_address": "1.1.1.1",
                    "prometheus_scrape_unit_name": "provider-app/0",
                },
            },
        )
        container = _container()
        return State(containers=[container], relations=[rel])

    def test_ca_file(self, context):
        scrape_jobs = [
            {
                "job_name": "job1",
                "static_configs": [{"targets": ["*:80"]}],
                "tls_config": {"ca_file": "CA 1"},
            },
            {
                "job_name": "job2",
                "static_configs": [{"targets": ["*:80"]}],
                "tls_config": {
                    "ca_file": "CA 2",
                    "cert_file": "CLIENT CERT 2",
                    "key_file": "CLIENT KEY 2",
                },
            },
        ]
        state_in = self._make_state(scrape_jobs)
        state_out = context.run(context.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ActiveStatus)
        fs = state_out.get_container("prometheus").get_filesystem(context)
        assert (fs / "etc/prometheus/job1-ca.crt").read_text() == "CA 1"
        assert (fs / "etc/prometheus/job2-ca.crt").read_text() == "CA 2"
        assert (fs / "etc/prometheus/job2-client.crt").read_text() == "CLIENT CERT 2"
        assert (fs / "etc/prometheus/job2-client.key").read_text() == "CLIENT KEY 2"

    def test_no_tls_config(self, context):
        scrape_jobs = [
            {
                "job_name": "job1",
                "static_configs": [{"targets": ["*:80"]}],
            },
        ]
        state_in = self._make_state(scrape_jobs)
        state_out = context.run(context.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ActiveStatus)

    def test_tls_config_missing_cert(self, context):
        scrape_jobs = [
            {
                "job_name": "job1",
                "static_configs": [{"targets": ["*:80"]}],
                "tls_config": {
                    "ca_file": "ca_file.pem",
                    "key_file": "private.key",
                },
            },
        ]
        state_in = self._make_state(scrape_jobs)
        state_out = context.run(context.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ActiveStatus)

    def test_tls_config_missing_key(self, context):
        scrape_jobs = [
            {
                "job_name": "job1",
                "static_configs": [{"targets": ["*:80"]}],
                "tls_config": {
                    "ca_file": "ca_file.pem",
                    "cert_file": "cert.pem",
                },
            },
        ]
        state_in = self._make_state(scrape_jobs)
        state_out = context.run(context.on.config_changed(), state_in)

        assert isinstance(state_out.unit_status, ActiveStatus)
