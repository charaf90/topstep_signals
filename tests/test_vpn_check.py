"""Tests pour broker/vpn_check — garde-fou anti-VPN sur Topstep."""

from __future__ import annotations

import pytest

from broker.vpn_check import (
    TAILSCALE_RANGE,
    check_no_vpn_on_topstep,
    is_ip_in_tailscale_range,
)

# ──────────────────────────────────────────────────────────────────────────────
# is_ip_in_tailscale_range
# ──────────────────────────────────────────────────────────────────────────────


class TestIsIpInTailscaleRange:

    def test_ip_isp_classique_hors_plage(self):
        assert is_ip_in_tailscale_range("192.168.1.1") is False
        assert is_ip_in_tailscale_range("88.120.45.66") is False
        assert is_ip_in_tailscale_range("8.8.8.8") is False
        assert is_ip_in_tailscale_range("203.0.113.5") is False

    def test_ip_tailscale_dans_plage(self):
        # 100.64.0.0/10 → 100.64.0.0 - 100.127.255.255
        assert is_ip_in_tailscale_range("100.64.0.0") is True
        assert is_ip_in_tailscale_range("100.100.50.42") is True
        assert is_ip_in_tailscale_range("100.127.255.255") is True

    def test_borders_juste_hors_plage(self):
        # 100.63.255.255 est juste avant la plage CGNAT
        assert is_ip_in_tailscale_range("100.63.255.255") is False
        # 100.128.0.0 est juste après
        assert is_ip_in_tailscale_range("100.128.0.0") is False

    def test_ip_invalide_retourne_false(self):
        assert is_ip_in_tailscale_range("pas une ip") is False
        assert is_ip_in_tailscale_range("999.999.999.999") is False
        assert is_ip_in_tailscale_range("") is False

    def test_plage_constante_correcte(self):
        """100.64.0.0/10 = plage CGNAT officielle utilisée par Tailscale."""
        assert str(TAILSCALE_RANGE) == "100.64.0.0/10"


# ──────────────────────────────────────────────────────────────────────────────
# check_no_vpn_on_topstep
# ──────────────────────────────────────────────────────────────────────────────


class TestCheckNoVpnOnTopstep:

    def test_ip_normale_passe(self):
        """IP ISP classique → check passe, retourne l'IP."""
        result = check_no_vpn_on_topstep(fetch_ip_fn=lambda: "88.120.45.66")
        assert result == "88.120.45.66"

    def test_ip_tailscale_leve(self):
        """IP dans plage Tailscale → RuntimeError."""
        with pytest.raises(RuntimeError, match="100.64.0.5"):
            check_no_vpn_on_topstep(fetch_ip_fn=lambda: "100.64.0.5")

    def test_message_erreur_explicite(self):
        """Le message d'erreur doit guider l'utilisateur."""
        with pytest.raises(RuntimeError) as exc_info:
            check_no_vpn_on_topstep(fetch_ip_fn=lambda: "100.100.50.42")
        msg = str(exc_info.value)
        assert "Tailscale" in msg
        assert "exit-node" in msg
        assert "ROADMAP_SOLO #8" in msg
        assert "tailscale up --reset" in msg

    def test_fetch_fail_passe_best_effort(self):
        """Si la résolution IP échoue, on laisse passer (best-effort)."""

        def fail():
            raise ConnectionError("no internet")

        result = check_no_vpn_on_topstep(fetch_ip_fn=fail)
        assert result is None

    def test_on_violation_callback_appele(self):
        """Le callback on_violation est appelé avant la levée."""
        captured = {}

        def callback(ip, msg):
            captured["ip"] = ip
            captured["msg"] = msg

        with pytest.raises(RuntimeError):
            check_no_vpn_on_topstep(
                fetch_ip_fn=lambda: "100.100.50.42",
                on_violation=callback,
            )
        assert captured["ip"] == "100.100.50.42"
        assert "Tailscale" in captured["msg"]

    def test_on_violation_qui_plante_ne_bloque_pas_la_levee(self):
        """Si le callback Telegram plante, la RuntimeError doit quand même être levée."""

        def broken_callback(ip, msg):
            raise ConnectionError("telegram down")

        with pytest.raises(RuntimeError, match="100.64.1.1"):
            check_no_vpn_on_topstep(
                fetch_ip_fn=lambda: "100.64.1.1",
                on_violation=broken_callback,
            )

    def test_callback_pas_appele_quand_ok(self):
        """Si l'IP est OK, on_violation n'est pas appelé."""
        called = []

        def callback(ip, msg):
            called.append((ip, msg))

        result = check_no_vpn_on_topstep(
            fetch_ip_fn=lambda: "88.120.45.66",
            on_violation=callback,
        )
        assert result == "88.120.45.66"
        assert called == []
