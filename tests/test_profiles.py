from gateway.app.profiles import AwgParameters, build_client_profile, qr_png


def test_profile_contains_amneziawg_parameters_and_encodes_as_png():
    """Removing AWG camouflage fields must break a mobile-importable profile."""
    profile = build_client_profile(
        "phone",
        "client-private",
        "10.77.0.2/32",
        "server-public",
        "191.44.45.36:585",
        AwgParameters(5, 8, 80, 31, 97, 11, 12, 13, 14),
    )

    assert "Jc = 5" in profile
    assert "H4 = 14" in profile
    assert "Endpoint = 191.44.45.36:585" in profile
    assert qr_png(profile).startswith(b"\x89PNG")
