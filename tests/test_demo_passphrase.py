from agent_commerce.demo.passphrase import check_passphrase


def test_correct_passphrase_passes() -> None:
    assert check_passphrase("open-sesame", "open-sesame") is True


def test_wrong_passphrase_fails() -> None:
    assert check_passphrase("wrong", "open-sesame") is False


def test_empty_entered_fails() -> None:
    assert check_passphrase("", "open-sesame") is False


def test_unset_expected_always_fails_closed() -> None:
    # No DEMO_PASSPHRASE configured must mean the gate can never open, not "gate disabled".
    assert check_passphrase("anything", "") is False
    assert check_passphrase("", "") is False
