from collections.abc import Callable

import pytest

from evil_captcha.certificate import InvalidCertificate, Notary, generate_key

STATEMENT = "Ada Lovelace has proven to be human on evil-captcha.org"


def test_certified_statement_verifies_with_a_notary_from_the_same_key(
    notary: Notary, key: str
) -> None:
    message = notary.certify(STATEMENT)

    assert message.startswith("-----BEGIN PGP SIGNED MESSAGE-----")
    assert STATEMENT in message
    assert Notary(key).verify(message) == STATEMENT
    assert Notary(key).public_key == notary.public_key


def test_key_that_is_not_a_pgp_key_is_refused() -> None:
    with pytest.raises(ValueError, match="PGP_KEY"):
        Notary("not a key")


def altered(message: str) -> str:
    return message.replace("Ada Lovelace", "Bob Builder")


def garbage(message: str) -> str:
    return "just some text"


@pytest.mark.parametrize("tamper", [altered, garbage])
def test_tampered_certificate_is_rejected(notary: Notary, tamper: Callable[[str], str]) -> None:
    message = tamper(notary.certify(STATEMENT))

    with pytest.raises(InvalidCertificate):
        notary.verify(message)


def test_certificate_from_another_key_is_rejected(notary: Notary) -> None:
    forged = Notary(generate_key()).certify(STATEMENT)

    with pytest.raises(InvalidCertificate):
        notary.verify(forged)
