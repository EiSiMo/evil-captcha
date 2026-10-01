"""Certificates of humanity: statements clearsigned with OpenPGP.

A ``Notary`` holds the site's PGP key, given as one base64 line (the armored
secret key, encoded so it fits in ``.env``). It certifies a statement such as
"Ada has proven to be human on evil-captcha.org" and verifies that a message is
such a statement, signed by this key and unaltered. Anyone can check a
certificate with ``gpg --verify`` after importing the public key.
"""

import base64
import binascii

import pysequoia

USER_ID = "evilCAPTCHA (evil-captcha.org)"


class InvalidCertificate(Exception):
    """The message is not an unaltered statement signed by this notary."""


def generate_key() -> str:
    """A new PGP key for the notary, as one base64 line."""
    return base64.b64encode(str(pysequoia.Tsk.generate(USER_ID)).encode()).decode()


class Notary:
    def __init__(self, key: str) -> None:
        try:
            self._key = pysequoia.Tsk.from_bytes(base64.b64decode(key, validate=True))
        except (binascii.Error, RuntimeError) as error:
            raise ValueError("PGP_KEY is not a base64 PGP secret key") from error
        self._certificate = self._key.extract_certificate()
        self.public_key = str(self._certificate)

    def certify(self, statement: str) -> str:
        """The statement, clearsigned."""
        signed = pysequoia.sign(
            self._key.signer(), statement.encode(), mode=pysequoia.SignatureMode.CLEAR
        )
        return signed.decode()

    def verify(self, message: str) -> str:
        """The statement a message certifies, if this notary signed it."""
        try:
            verified = pysequoia.verify(bytes=message.encode(), store=self._certificates)
        except RuntimeError as error:
            raise InvalidCertificate(str(error)) from error
        if not verified.valid_sigs or verified.bytes is None:
            raise InvalidCertificate("no valid signature")
        return verified.bytes.decode()

    def _certificates(self, _key_ids: list[str]) -> list[pysequoia.Cert]:
        return [self._certificate]
