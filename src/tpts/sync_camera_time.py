from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import unquote


LOGGER = logging.getLogger(__name__)
DER_TAG_SEQUENCE = 0x30
DER_TAG_INTEGER = 0x02
DER_TAG_BIT_STRING = 0x03
RSA_PKCS1_V15_MIN_PADDING = 11
HTTP_TIMEOUT_SECONDS = 10


def tp_encrypt(password: str) -> str:
    TP_XOR_SEED = "RDpbLfCPsJZ7fiv"
    TP_XOR_ALPHABET = (
        "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4r"
        "BL3cD02KZciXTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro5"
        "10qXeU07kV57fQMc8L6aLgMLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZ"
        "LEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW "
    )
    result_chars: list[str] = []
    max_len = max(len(TP_XOR_SEED), len(password))
    alphabet_len = len(TP_XOR_ALPHABET)

    for idx in range(max_len):
        seed_code = ord(TP_XOR_SEED[idx]) if idx < len(TP_XOR_SEED) else 187
        password_code = ord(password[idx]) if idx < len(password) else 187
        mixed_index = (seed_code ^ password_code) % alphabet_len
        result_chars.append(TP_XOR_ALPHABET[mixed_index])

    return "".join(result_chars)


def read_der_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("invalid DER length")

    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset

    length_len = first & 0x7F
    if length_len == 0 or offset + length_len > len(data):
        raise ValueError("invalid DER long-form length")

    length = int.from_bytes(data[offset : offset + length_len], "big")
    return length, offset + length_len


def read_der_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    if offset >= len(data):
        raise ValueError("invalid DER TLV")

    tag = data[offset]
    length, value_offset = read_der_length(data, offset + 1)
    end = value_offset + length
    if end > len(data):
        raise ValueError("invalid DER value length")

    return tag, data[value_offset:end], end


def parse_rsa_pubkey_der(der_bytes: bytes) -> tuple[str, str]:
    tag, seq_value, seq_end = read_der_tlv(der_bytes, 0)
    if tag != DER_TAG_SEQUENCE or seq_end != len(der_bytes):
        raise ValueError("invalid RSA public key DER")

    offset = 0
    tag, modulus, offset = read_der_tlv(seq_value, offset)
    if tag != DER_TAG_INTEGER:
        raise ValueError("invalid RSA modulus")

    tag, exponent, offset = read_der_tlv(seq_value, offset)
    if tag != DER_TAG_INTEGER:
        raise ValueError("invalid RSA exponent")

    if offset != len(seq_value):
        raise ValueError("invalid RSA key trailing data")

    return modulus.hex(), exponent.hex()


def parse_spki_rsa_der(der_bytes: bytes) -> tuple[str, str]:
    tag, seq_value, seq_end = read_der_tlv(der_bytes, 0)
    if tag != DER_TAG_SEQUENCE or seq_end != len(der_bytes):
        raise ValueError("invalid SPKI DER")

    offset = 0
    tag, _, offset = read_der_tlv(seq_value, offset)
    if tag != DER_TAG_SEQUENCE:
        raise ValueError("invalid SPKI algorithm identifier")

    tag, bit_string, offset = read_der_tlv(seq_value, offset)
    if tag != DER_TAG_BIT_STRING or not bit_string:
        raise ValueError("invalid SPKI bit string")

    if offset != len(seq_value):
        raise ValueError("invalid SPKI trailing data")

    if bit_string[0] != 0:
        raise ValueError("unsupported SPKI unused bits")

    return parse_rsa_pubkey_der(bit_string[1:])


def convert_rsa_key(public_key_b64: str) -> tuple[str, str]:
    der_bytes = base64.b64decode(public_key_b64)
    try:
        return parse_spki_rsa_der(der_bytes)
    except ValueError:
        return parse_rsa_pubkey_der(der_bytes)


def rsa_encrypt_pkcs1_v15(plaintext: str, public_key_b64: str) -> str:
    modulus_hex, exponent_hex = convert_rsa_key(public_key_b64)
    modulus = int(modulus_hex, 16)
    exponent = int(exponent_hex, 16)
    key_length = (modulus.bit_length() + 7) // 8

    message = plaintext.encode("utf-8")
    if len(message) > key_length - RSA_PKCS1_V15_MIN_PADDING:
        raise ValueError("message too long for RSA key size")

    padding_len = key_length - len(message) - 3
    padding = b""
    while len(padding) < padding_len:
        padding += os.urandom(padding_len - len(padding)).replace(b"\x00", b"")
    padding = padding[:padding_len]

    padded_message = b"\x00\x02" + padding + b"\x00" + message
    message_int = int.from_bytes(padded_message, "big")
    cipher_int = pow(message_int, exponent, modulus)
    cipher_bytes = cipher_int.to_bytes(key_length, "big")
    return base64.b64encode(cipher_bytes).decode("utf-8")


def post_json(base_url: str, payload: dict[str, Any], stok: str = "") -> dict[str, Any]:
    endpoint = f"{base_url}/stok={stok}/ds" if stok else base_url
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )

    response_body = b""
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        if not response_body:
            raise RuntimeError(
                f"device returned HTTP {exc.code} for {endpoint} without a response body"
            ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unable to reach device API at {endpoint}: {exc.reason}") from exc

    try:
        parsed = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("device returned non-JSON response") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("device returned a non-object JSON response")

    return parsed


def get_stok(base_url: str, username: str, password: str) -> str:
    init_resp = post_json(base_url, {"method": "do", "login": {}})
    data = init_resp.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("device login init response is missing data")

    public_key = data.get("key")
    nonce = data.get("nonce")
    if not isinstance(public_key, str) or not public_key:
        raise RuntimeError("device login init response is missing public key")
    if nonce is None:
        raise RuntimeError("device login init response is missing nonce")

    login_secret = f"{tp_encrypt(password)}:{nonce}"
    rsa_password = rsa_encrypt_pkcs1_v15(login_secret, unquote(public_key))
    login_resp = post_json(
        base_url,
        {
            "method": "do",
            "login": {
                "username": username,
                "encrypt_type": "2",
                "password": rsa_password,
            },
        },
    )
    stok = login_resp.get("stok")
    if not isinstance(stok, str) or not stok:
        raise RuntimeError(f"device login failed: {login_resp}")
    return stok


def sync_device_time(base_url: str, stok: str) -> dict[str, Any]:
    timestamp = int(time.time())
    response = post_json(
        base_url,
        {
            "system": {"boot_set_date": {"seconds_from_1970": timestamp}},
            "method": "do",
        },
        stok,
    )
    error_code = response.get("error_code")
    if error_code not in (None, 0):
        raise RuntimeError(f"device rejected time sync: {response}")
    return response


def sync_camera_time(username: str, password: str, base_url: str) -> dict[str, Any]:
    stok = get_stok(base_url=base_url, username=username, password=password)
    LOGGER.info("Authenticated to %s", base_url)
    response = sync_device_time(base_url=base_url, stok=stok)
    LOGGER.info("Time sync response from %s: %s", base_url, response)
    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync TP-Link IPC API device time.")
    parser.add_argument("username", help="Device username")
    parser.add_argument("password", help="Device password")
    parser.add_argument("base_url", help="Device API base URL, e.g. http://192.168.0.1")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    LOGGER.info("Starting time sync for %s as %s", args.base_url, args.username)
    sync_camera_time(args.username, args.password, args.base_url)
    LOGGER.info("Done")
    return 0
