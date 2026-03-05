# Copyright 2020 Canonical Ltd.
# See LICENSE file for licensing details.

import dataclasses
import json
import logging
import socket
import uuid
from unittest.mock import patch

import yaml
from helpers import cli_arg
from ops import pebble
from ops.testing import Container, Exec, Relation, State

import pytest

from charm import PROMETHEUS_CONFIG, PrometheusCharm


@pytest.fixture(autouse=True)
def _patch_pvc_capacity():
    with patch.object(PrometheusCharm, "_get_pvc_capacity", return_value="1Gi"):
        yield

logger = logging.getLogger(__name__)

RELATION_NAME = "metrics-endpoint"
DEFAULT_JOBS = [{"metrics_path": "/metrics"}]
SCRAPE_METADATA = {
    "model": "provider-model",
    "model_uuid": str(uuid.uuid4()),
    "application": "provider",
    "charm_name": "provider-charm",
}


def _prometheus_container():
    return Container(
        "prometheus",
        can_connect=True,
        layers={"prometheus": pebble.Layer({"services": {"prometheus": {}}})},
        service_statuses={"prometheus": pebble.ServiceStatus.INACTIVE},
        execs={Exec(["update-ca-certificates", "--fresh"], return_code=0, stdout="")},
    )


def _get_plan(state_out, context):
    """Extract the pebble plan from the output state by merging layers."""
    container = state_out.get_container("prometheus")
    plan = pebble.Plan()
    for layer in container.layers.values():
        plan = plan.to_dict()
        layer_dict = layer.to_dict()
        # Merge: last layer wins for services
        merged = {**plan}
        for key in layer_dict:
            if key == "services":
                merged.setdefault("services", {})
                merged["services"].update(layer_dict["services"])
            else:
                merged[key] = layer_dict[key]
        plan = pebble.Plan(yaml.safe_dump(merged))
    return plan


class TestCharm:
    def test_grafana_is_provided_port_and_source(self, context, prometheus_container):
        rel = Relation("grafana-source", remote_app_name="grafana")
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        state_out = context.run(context.on.relation_joined(rel), state)
        fqdn = socket.getfqdn()
        rel_out = state_out.get_relation(rel.id)
        grafana_host = rel_out.local_unit_data.get("grafana_source_host")
        assert grafana_host == "http://{}:{}".format(fqdn, "9090")

    def test_default_cli_log_level_is_info(self, context, prometheus_container):
        state = State(leader=True, containers=[prometheus_container])
        state_out = context.run(context.on.pebble_ready(prometheus_container), state)
        plan = _get_plan(state_out, context)
        assert cli_arg(plan, "--log.level") == "info"

    def test_invalid_log_level_defaults_to_debug(self, context, prometheus_container):
        state = State(
            leader=True,
            containers=[prometheus_container],
            config={"log_level": "bad-level"},
        )
        state_out = context.run(context.on.config_changed(), state)
        plan = _get_plan(state_out, context)
        assert cli_arg(plan, "--log.level") == "debug"

    def test_valid_log_level_is_accepted(self, context, prometheus_container):
        state = State(
            leader=True,
            containers=[prometheus_container],
            config={"log_level": "warn"},
        )
        state_out = context.run(context.on.config_changed(), state)
        plan = _get_plan(state_out, context)
        assert cli_arg(plan, "--log.level") == "warn"

    def test_ingress_relation_not_set(self, context, prometheus_container):
        state = State(leader=True, containers=[prometheus_container])
        state_out = context.run(context.on.pebble_ready(prometheus_container), state)
        plan = _get_plan(state_out, context)
        fqdn = socket.getfqdn()
        assert cli_arg(plan, "--web.external-url") == f"http://{fqdn}:9090"

    def test_ingress_relation_set(self, context, prometheus_container):
        ingress_rel = Relation(
            "ingress",
            remote_app_name="traefik-ingress",
            remote_app_data={
                "ingress": yaml.safe_dump(
                    {"prometheus-k8s/0": {"url": "http://test:80"}}
                )
            },
        )
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[ingress_rel],
        )
        state_out = context.run(context.on.relation_changed(ingress_rel), state)
        plan = _get_plan(state_out, context)
        assert cli_arg(plan, "--web.external-url") == "http://test:80"

    def test_web_external_has_no_effect(self, context, prometheus_container):
        state = State(
            leader=True,
            containers=[prometheus_container],
            config={"web_external_url": "http://test:80/sub/path"},
        )
        state_out = context.run(context.on.config_changed(), state)
        plan = _get_plan(state_out, context)
        fqdn = socket.getfqdn()
        assert cli_arg(plan, "--web.external-url") == f"http://{fqdn}:9090"

    def test_metrics_wal_compression_is_not_enabled_by_default(
        self, context, prometheus_container
    ):
        state = State(leader=True, containers=[prometheus_container])
        state_out = context.run(context.on.pebble_ready(prometheus_container), state)
        plan = _get_plan(state_out, context)
        assert cli_arg(plan, "--storage.tsdb.wal-compression") is None

    def test_metrics_wal_compression_can_be_enabled(self, context, prometheus_container):
        state = State(
            leader=True,
            containers=[prometheus_container],
            config={"metrics_wal_compression": True},
        )
        state_out = context.run(context.on.config_changed(), state)
        plan = _get_plan(state_out, context)
        assert (
            cli_arg(plan, "--storage.tsdb.wal-compression")
            == "--storage.tsdb.wal-compression"
        )

    def test_valid_metrics_retention_times_can_be_set(
        self, context, prometheus_container
    ):
        acceptable_units = ["y", "w", "d", "h", "m", "s"]
        for unit in acceptable_units:
            retention_time = "{}{}".format(1, unit)
            state = State(
                leader=True,
                containers=[prometheus_container],
                config={"metrics_retention_time": retention_time},
            )
            state_out = context.run(context.on.config_changed(), state)
            plan = _get_plan(state_out, context)
            assert cli_arg(plan, "--storage.tsdb.retention.time") == retention_time

    def test_invalid_metrics_retention_times_can_not_be_set(
        self, context, prometheus_container
    ):
        for retention_time in ["1x", "5m1y2d"]:
            state = State(
                leader=True,
                containers=[prometheus_container],
                config={"metrics_retention_time": retention_time},
            )
            state_out = context.run(context.on.config_changed(), state)
            plan = _get_plan(state_out, context)
            assert cli_arg(plan, "--storage.tsdb.retention.time") is None

    def test_global_evaluation_interval_can_be_set(
        self, context, prometheus_container
    ):
        acceptable_units = ["y", "w", "d", "h", "m", "s"]
        for unit in acceptable_units:
            eval_interval = "{}{}".format(1, unit)
            state = State(
                leader=True,
                containers=[prometheus_container],
                config={"evaluation_interval": eval_interval},
            )
            state_out = context.run(context.on.config_changed(), state)
            container = state_out.get_container("prometheus")
            fs = container.get_filesystem(context)
            config_path = fs / PROMETHEUS_CONFIG.lstrip("/")
            config_dict = yaml.safe_load(config_path.read_text())
            assert config_dict["global"]["evaluation_interval"] == eval_interval

    def test_default_scrape_config_is_always_set(self, context, prometheus_container):
        state = State(leader=True, containers=[prometheus_container])
        state_out = context.run(context.on.pebble_ready(prometheus_container), state)
        container = state_out.get_container("prometheus")
        fs = container.get_filesystem(context)
        config_path = fs / PROMETHEUS_CONFIG.lstrip("/")
        config_dict = yaml.safe_load(config_path.read_text())
        scrape_configs = config_dict["scrape_configs"]
        prom_config = None
        for sc in scrape_configs:
            if sc["job_name"] == "prometheus":
                prom_config = sc
                break
        assert prom_config is not None, "No default config found"

    def test_honor_labels_is_always_set_in_scrape_configs(
        self, context, prometheus_container
    ):
        rel = Relation(
            RELATION_NAME,
            remote_app_name="provider",
            remote_app_data={
                "scrape_metadata": json.dumps(SCRAPE_METADATA),
                "scrape_jobs": json.dumps(DEFAULT_JOBS),
            },
        )
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        state_out = context.run(context.on.relation_changed(rel), state)
        container = state_out.get_container("prometheus")
        fs = container.get_filesystem(context)
        config_path = fs / PROMETHEUS_CONFIG.lstrip("/")
        config_dict = yaml.safe_load(config_path.read_text())
        for job in config_dict["scrape_configs"]:
            if job["job_name"] != "prometheus":
                assert "honor_labels" in job
                assert job["honor_labels"] is True

    def _get_running_state(self, context, prometheus_container):
        """Get state after initial pebble_ready (prometheus running with a plan)."""
        state = State(leader=True, containers=[prometheus_container])
        return context.run(context.on.pebble_ready(prometheus_container), state)

    def test_configuration_reload(self, context, prometheus_container):
        running_state = self._get_running_state(context, prometheus_container)
        state_with_config = dataclasses.replace(
            running_state,
            config={"evaluation_interval": "1234m"},
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration"
        ) as reload_mock:
            context.run(context.on.config_changed(), state_with_config)
            reload_mock.assert_called()

    def test_configuration_reload_success(self, context, prometheus_container):
        running_state = self._get_running_state(context, prometheus_container)
        state_with_config = dataclasses.replace(
            running_state,
            config={"evaluation_interval": "1234m"},
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.config_changed(), state_with_config)
        assert state_out.unit_status.name == "active"

    def test_configuration_reload_error(self, context, prometheus_container):
        running_state = self._get_running_state(context, prometheus_container)
        state_with_config = dataclasses.replace(
            running_state,
            config={"evaluation_interval": "1234m"},
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=False
        ):
            state_out = context.run(context.on.config_changed(), state_with_config)
        assert state_out.unit_status.name == "blocked"

    def test_configuration_reload_read_timeout(self, context, prometheus_container):
        running_state = self._get_running_state(context, prometheus_container)
        state_with_config = dataclasses.replace(
            running_state,
            config={"evaluation_interval": "1234m"},
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration",
            return_value="read_timeout",
        ):
            state_out = context.run(context.on.config_changed(), state_with_config)
        assert state_out.unit_status.name == "maintenance"


def _scrape_config(config_yaml, job_name):
    config_dict = yaml.safe_load(config_yaml)
    scrape_configs = config_dict["scrape_configs"]
    for config in scrape_configs:
        if config["job_name"] == job_name:
            return config
    return None


class TestConfigMaximumRetentionSize:
    """Test the charmcraft.yaml option 'maximum_retention_size'."""

    def test_default_maximum_retention_size_is_80_percent(
        self, context, prometheus_container
    ):
        """Backwards-compat: default maximum_retention_size is 80%."""
        state = State(leader=True, containers=[prometheus_container])
        state_out = context.run(context.on.pebble_ready(prometheus_container), state)
        plan = _get_plan(state_out, context)
        assert cli_arg(plan, "--storage.tsdb.retention.size") == "0.8GB"

    def test_multiplication_factor_applied_to_pvc_capacity(
        self, context, prometheus_container
    ):
        """The `--storage.tsdb.retention.size` arg must be multiplied by maximum_retention_size."""
        for set_point, read_back in [("0%", "0GB"), ("50%", "0.5GB"), ("100%", "1GB")]:
            state = State(
                leader=True,
                containers=[prometheus_container],
                config={"maximum_retention_size": set_point},
            )
            state_out = context.run(context.on.config_changed(), state)
            plan = _get_plan(state_out, context)
            assert cli_arg(plan, "--storage.tsdb.retention.size") == read_back

    def test_invalid_retention_size_config_option_string(
        self, context, prometheus_container
    ):
        # Invalid: no percent sign
        state = State(
            leader=True,
            containers=[prometheus_container],
            config={"maximum_retention_size": "42"},
        )
        state_out = context.run(context.on.config_changed(), state)
        plan = _get_plan(state_out, context)
        assert cli_arg(plan, "--storage.tsdb.retention.size") is None
        assert state_out.unit_status.name == "blocked"

        # Invalid: wrong suffix
        state2 = State(
            leader=True,
            containers=[prometheus_container],
            config={"maximum_retention_size": "4GiB"},
        )
        state_out2 = context.run(context.on.config_changed(), state2)
        plan2 = _get_plan(state_out2, context)
        assert cli_arg(plan2, "--storage.tsdb.retention.size") is None
        assert state_out2.unit_status.name == "blocked"

        # Valid: correct format
        state3 = State(
            leader=True,
            containers=[prometheus_container],
            config={"maximum_retention_size": "42%"},
        )
        state_out3 = context.run(context.on.config_changed(), state3)
        plan3 = _get_plan(state_out3, context)
        assert cli_arg(plan3, "--storage.tsdb.retention.size") == "0.42GB"
        assert state_out3.unit_status.name == "active"


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
    @staticmethod
    def _get_rules_files(state_out, context):
        container = state_out.get_container("prometheus")
        fs = container.get_filesystem(context)
        rules_dir = fs / "etc/prometheus/rules"
        if rules_dir.exists():
            return {
                f"/etc/prometheus/rules/{f.name}" for f in rules_dir.iterdir() if f.is_file()
            }
        return set()

    def test_charm_writes_meaningful_alerts_filename_1(
        self, context, prometheus_container
    ):
        """When relation data includes both scrape_metadata and labeled alerts."""
        rel = Relation(
            RELATION_NAME,
            remote_app_name="remote-app",
            remote_app_data={
                "scrape_metadata": json.dumps(REMOTE_SCRAPE_METADATA),
                "alert_rules": json.dumps(LABELED_ALERT_RULES),
            },
        )
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)
        files = self._get_rules_files(state_out, context)
        assert files == {
            f"/etc/prometheus/rules/juju_ZZZ-model_a5edc336_zzz-app_{RELATION_NAME}_{rel.id}.rules"
        }

    def test_charm_writes_meaningful_alerts_filename_2(
        self, context, prometheus_container
    ):
        """When relation data includes only labeled alerts (no scrape_metadata)."""
        rel = Relation(
            RELATION_NAME,
            remote_app_name="remote-app",
            remote_app_data={
                "alert_rules": json.dumps(LABELED_ALERT_RULES),
            },
        )
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)
        files = self._get_rules_files(state_out, context)
        assert files == {
            f"/etc/prometheus/rules/juju_ZZZ-model_a5edc336_zzz-app_{RELATION_NAME}_{rel.id}.rules"
        }

    def test_charm_writes_meaningful_alerts_filename_3(
        self, context, prometheus_container
    ):
        """When relation data includes scrape_metadata but _unlabeled_ alerts."""
        rel = Relation(
            RELATION_NAME,
            remote_app_name="remote-app",
            remote_app_data={
                "scrape_metadata": json.dumps(REMOTE_SCRAPE_METADATA),
                "alert_rules": json.dumps(UNLABELED_ALERT_RULES),
            },
        )
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)
        files = self._get_rules_files(state_out, context)
        assert files == {
            f"/etc/prometheus/rules/juju_remote-model_be44e4b8_remote-app_{RELATION_NAME}_{rel.id}.rules"
        }

    def test_charm_writes_meaningful_alerts_filename_4(
        self, context, prometheus_container
    ):
        """When relation data includes only _unlabeled_ alerts (no scrape_metadata)."""
        rel = Relation(
            RELATION_NAME,
            remote_app_name="remote-app",
            remote_app_data={
                "alert_rules": json.dumps(UNLABELED_ALERT_RULES),
            },
        )
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)
        files = self._get_rules_files(state_out, context)
        assert files == {
            f"/etc/prometheus/rules/juju_ZZZ_group_alerts_{RELATION_NAME}_{rel.id}.rules"
        }


class TestPebblePlan:
    """Test the pebble plan is kept up-to-date (situational awareness)."""

    def test_no_restart_nor_reload_when_nothing_changes(
        self, context, prometheus_container
    ):
        """When nothing changes, calling _configure shouldn't result in downtime."""
        state = State(leader=True, containers=[prometheus_container])
        # First, set up with pebble_ready to establish a plan
        state_out = context.run(
            context.on.pebble_ready(prometheus_container), state
        )
        initial_plan = _get_plan(state_out, context)

        # Run update_status - plan should not change
        with patch(
            "prometheus_client.Prometheus.reload_configuration"
        ) as reload_mock:
            # Use the output state containers for the next run
            state_out2 = context.run(context.on.update_status(), state_out)
            reload_mock.assert_not_called()

        current_plan = _get_plan(state_out2, context)
        assert initial_plan.to_dict() == current_plan.to_dict()

    def test_workload_hot_reloads_when_some_config_options_change(
        self, context, prometheus_container
    ):
        """Some config options go into the config file and require a reload (not restart)."""
        state = State(leader=True, containers=[prometheus_container])
        state_out = context.run(
            context.on.pebble_ready(prometheus_container), state
        )
        initial_plan = _get_plan(state_out, context)

        # Change evaluation_interval
        state_with_config = dataclasses.replace(
            state_out,
            config={"evaluation_interval": "1234s"},
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration"
        ) as reload_mock:
            state_out2 = context.run(
                context.on.config_changed(), state_with_config
            )
            reload_mock.assert_called()

        # Plan should not change (only config file changes, no pebble restart)
        current_plan = _get_plan(state_out2, context)
        assert initial_plan.to_dict() == current_plan.to_dict()


class TestTlsConfig:
    def test_ca_file(self, context, prometheus_container):
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
        rel = Relation(
            RELATION_NAME,
            remote_app_name="provider-app",
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
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)

        assert state_out.unit_status.name == "active"
        container = state_out.get_container("prometheus")
        fs = container.get_filesystem(context)
        assert (fs / "etc/prometheus/job1-ca.crt").read_text() == "CA 1"
        assert (fs / "etc/prometheus/job2-ca.crt").read_text() == "CA 2"
        assert (fs / "etc/prometheus/job2-client.crt").read_text() == "CLIENT CERT 2"
        assert (fs / "etc/prometheus/job2-client.key").read_text() == "CLIENT KEY 2"

    def test_no_tls_config(self, context, prometheus_container):
        scrape_jobs = [
            {
                "job_name": "job1",
                "static_configs": [{"targets": ["*:80"]}],
            },
        ]
        rel = Relation(
            RELATION_NAME,
            remote_app_name="provider-app",
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
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)
        assert state_out.unit_status.name == "active"

    def test_tls_config_missing_cert(self, context, prometheus_container):
        """When tls_config has key_file but no cert_file, the invalid job is dropped."""
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
        rel = Relation(
            RELATION_NAME,
            remote_app_name="provider-app",
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
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)
        assert state_out.unit_status.name == "active"

    def test_tls_config_missing_key(self, context, prometheus_container):
        """When tls_config has cert_file but no key_file, the invalid job is dropped."""
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
        rel = Relation(
            RELATION_NAME,
            remote_app_name="provider-app",
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
        state = State(
            leader=True,
            containers=[prometheus_container],
            relations=[rel],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(context.on.relation_changed(rel), state)
        assert state_out.unit_status.name == "active"
