#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import datetime
import logging
from unittest.mock import Mock, patch

import pytest
from ops.pebble import Change, ChangeError, ChangeID
from ops.testing import State

from charm import PrometheusCharm

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _patch_pvc_capacity():
    with patch.object(PrometheusCharm, "_get_pvc_capacity", return_value="1Gi"):
        yield


class TestActiveStatus:
    """Feature: Charm's status should reflect the correctness of the config / relations.

    Background: When launched on its own, the charm should always end up with active status.
    In some cases (e.g. Ingress conflicts) the charm should go into blocked state.
    """

    def test_unit_is_active_if_deployed_without_relations_or_config(
        self, context, prometheus_container
    ):
        """Scenario: Unit is deployed without any user-provided config or regular relations."""
        state = State(
            leader=True,
            containers=[prometheus_container],
        )
        with patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=True
        ):
            state_out = context.run(
                context.on.pebble_ready(prometheus_container), state
            )

        assert state_out.unit_status.name == "active"

        # Pebble plan is not empty
        container = state_out.get_container("prometheus")
        has_layers = any(layer.to_dict() for layer in container.layers.values())
        assert has_layers

    def test_unit_is_blocked_if_reload_configuration_fails(
        self, context, prometheus_container
    ):
        """Scenario: Unit is deployed but reload configuration fails."""
        state = State(
            leader=True,
            containers=[prometheus_container],
        )
        cid = ChangeID("0")
        spawn_time = datetime.datetime.now()
        change = Change(
            cid, "kind", "summary", "status", [], False, None, spawn_time, None
        )
        with patch(
            "ops.model.Container.replan",
            Mock(side_effect=ChangeError("err", change)),
        ), patch(
            "prometheus_client.Prometheus.reload_configuration", return_value=False
        ):
            state_out = context.run(
                context.on.pebble_ready(prometheus_container), state
            )

        assert state_out.unit_status.name == "blocked"

        # Pebble plan is not empty
        container = state_out.get_container("prometheus")
        has_layers = any(layer.to_dict() for layer in container.layers.values())
        assert has_layers
