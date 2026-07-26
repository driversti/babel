import pytest

from babel.vpn import IpInfo, IpLeak, check_ip_leak, is_leaking


def test_is_leaking_is_case_insensitive():
    assert is_leaking(IpInfo(ip="1.2.3.4", country="pl"), "PL")
    assert not is_leaking(IpInfo(ip="1.2.3.4", country="DE"), "PL")


async def test_check_ip_leak_raises_on_home_country():
    async def fake_lookup() -> IpInfo:
        return IpInfo(ip="5.6.7.8", country="PL")

    with pytest.raises(IpLeak):
        await check_ip_leak(home_country="PL", lookup=fake_lookup)


async def test_check_ip_leak_returns_info_when_clean():
    async def fake_lookup() -> IpInfo:
        return IpInfo(ip="9.9.9.9", country="DE")

    info = await check_ip_leak(home_country="PL", lookup=fake_lookup)
    assert info.country == "DE"


async def test_check_ip_leak_retries_transient_failures():
    calls = {"n": 0}

    async def flaky() -> IpInfo:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("provider down")
        return IpInfo(ip="9.9.9.9", country="DE")

    info = await check_ip_leak(home_country="PL", lookup=flaky, retries=5, backoff_sec=0)
    assert info.ip == "9.9.9.9"
    assert calls["n"] == 3
