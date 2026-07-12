"""Monkeypatch: reassemble chunked V1 map responses.

python-roborock (<=5.29.x) assumes a map response fits in one MQTT frame.
Large maps (observed on a Qrevo a288 with a ~1.1MB whole-house map) arrive
as one header-bearing frame (~64KB) plus continuation frames of raw
ciphertext. Upstream tries to parse a continuation frame's bytes as a
header, crashes on `endpoint.decode()`, and the fetch times out.

This module replaces `create_map_response_decoder` with a version that
buffers frames per decoder and returns the map once the concatenated
ciphertext decrypts to a complete gzip stream. Continuation frames may
arrive before the header frame; ciphertext order is header-body first,
continuations appended in arrival order.

Import this module before creating any devices. Safe on maps that fit in
a single frame.
"""

import logging
import struct
import zlib
from collections.abc import Callable

from roborock.protocol import Utils
from roborock.protocols import v1_protocol
from roborock.protocols.v1_protocol import MapResponse, SecurityData
from roborock.roborock_message import RoborockMessage

_LOGGER = logging.getLogger(__name__)

_MAX_BUFFERED_FRAMES = 32


def _create_map_response_decoder(security_data: SecurityData) -> Callable[[RoborockMessage], MapResponse | None]:
    state: dict = {"main": None, "continuations": []}

    def _try_assemble() -> MapResponse | None:
        if state["main"] is None:
            return None
        request_id, main_body = state["main"]
        candidates = [main_body]
        acc = main_body
        for cont in state["continuations"]:
            acc = acc + cont
            candidates.append(acc)
        for ciphertext in candidates:
            if rem := len(ciphertext) % 16:
                ciphertext = ciphertext[: len(ciphertext) - rem]
            try:
                decrypted = Utils.decrypt_cbc(ciphertext, security_data.nonce)
            except ValueError:
                continue
            if not decrypted.startswith(b"\x1f\x8b"):
                continue
            decompressor = zlib.decompressobj(wbits=31)
            try:
                data = decompressor.decompress(decrypted)
            except zlib.error:
                continue
            if not decompressor.eof:
                continue  # gzip stream incomplete; wait for more frames
            state["main"] = None
            state["continuations"] = []
            return MapResponse(request_id=request_id, data=data)
        return None

    def _decode(message: RoborockMessage) -> MapResponse | None:
        if not message.payload or len(message.payload) < 24:
            return None
        header, body = message.payload[:24], message.payload[24:]
        [endpoint, _, request_id, _] = struct.unpack("<8s8sH6s", header)
        try:
            endpoint_matches = endpoint.decode().startswith(security_data.endpoint)
        except UnicodeDecodeError:
            endpoint_matches = False
        if endpoint_matches:
            state["main"] = (request_id, body)
            # continuations observed to arrive before the header frame; keep them
        elif message.payload.startswith(b"{"):
            return None  # JSON dps message, not map data
        else:
            state["continuations"].append(message.payload)
            del state["continuations"][:-_MAX_BUFFERED_FRAMES]
        return _try_assemble()

    return _decode


def apply() -> None:
    """Install the patched decoder into python-roborock."""
    v1_protocol.create_map_response_decoder = _create_map_response_decoder
    # v1_channel imports the symbol directly; patch its reference too
    from roborock.devices.rpc import v1_channel

    if hasattr(v1_channel, "create_map_response_decoder"):
        v1_channel.create_map_response_decoder = _create_map_response_decoder
    _LOGGER.debug("chunked map response decoder installed")


apply()
