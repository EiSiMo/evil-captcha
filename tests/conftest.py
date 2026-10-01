import pytest

from evil_captcha.certificate import Notary, generate_key


@pytest.fixture(scope="session")
def key() -> str:
    return generate_key()


@pytest.fixture(scope="session")
def notary(key: str) -> Notary:
    return Notary(key)
