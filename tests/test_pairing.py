from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from slack_conductor.pairing import PAIR_TTL_SECONDS, PairingStore


def _register(store, code, token, name="mac", backend="conductor"):
    store.register(code, sha256(token.encode()).hexdigest(), name, backend)


def test_pairing_is_single_use_and_survives_restart(tmp_path):
    path = tmp_path / "pairing.sqlite"
    store = PairingStore(path)
    code = "ab" * 16
    token = "private-bearer"
    _register(store, code, token)
    _register(store, code, token)
    with pytest.raises(ValueError):
        _register(store, code, "replacement-bearer")
    assert store.authenticate(token) is None

    restarted = PairingStore(path)
    profile = restarted.claim(code, "T1", "U1", event_key="Ev1")
    assert (profile.name, profile.backend) == ("mac", "conductor")
    assert restarted.authenticate(token) == profile
    assert restarted.get_default("T1", "U1") == profile
    assert restarted.profiles("T1", "U2") == []
    assert not restarted.revoke("T1", "U2", "mac")
    assert restarted.claim(code, "T1", "U1", event_key="Ev1") == profile
    with pytest.raises(ValueError):
        restarted.claim(code, "T1", "U2", event_key="Ev1")
    with pytest.raises(ValueError):
        restarted.claim(code, "T1", "U1", event_key="Ev2")
    assert restarted.revoke("T1", "U1", "mac")
    assert not restarted.is_active(profile.id)
    assert restarted.authenticate(token) is None
    with pytest.raises(ValueError):
        _register(restarted, "bc" * 16, token)
    with pytest.raises(ValueError):
        _register(restarted, code, "replacement-bearer")


def test_defaults_and_reused_name_keep_separate_profile_identities(tmp_path):
    store = PairingStore(tmp_path / "pairing.sqlite")
    _register(store, "11" * 16, "token-a")
    mac = store.claim("11" * 16, "T1", "U1")
    _register(store, "22" * 16, "token-b", "daytona", "headless")
    daytona = store.claim("22" * 16, "T1", "U1")
    _register(store, "33" * 16, "token-c")
    other_mac = store.claim("33" * 16, "T1", "U2")
    assert store.set_default("T1", "U1", "daytona") == daytona
    assert store.get_default("T1", "U1") == daytona
    assert store.get_default("T1", "U2") == other_mac
    assert store.revoke("T1", "U1", "daytona")
    assert store.get_default("T1", "U1") == mac
    _register(store, "44" * 16, "token-d", "daytona", "headless")
    replacement = store.claim("44" * 16, "T1", "U1")
    assert replacement.id != daytona.id
    assert store.get(daytona.id) == daytona
    assert store.find("T1", "U1", "daytona") == replacement
    assert store.list_active("T1") == [mac, other_mac, replacement]


def test_expiry_and_atomic_claim(tmp_path):
    now = 100
    store = PairingStore(tmp_path / "pairing.sqlite", clock=lambda: now)
    expired = "ab" * 16
    _register(store, expired, "token-a")
    now += PAIR_TTL_SECONDS
    with pytest.raises(ValueError):
        store.claim(expired, "T1", "U0")
    code = "bc" * 16
    _register(store, code, "token-b")

    def claim(user_id):
        try:
            return store.claim(code, "T1", user_id)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["U1", "U2"]))
    assert len([profile for profile in results if profile]) == 1
    assert store.authenticate("token-b") in results
