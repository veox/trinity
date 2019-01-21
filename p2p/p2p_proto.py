import enum
from typing import (
    cast,
    Any,
    Dict,
    TYPE_CHECKING
)

from eth_utils.toolz import assoc

import rlp
from rlp import sedes

from p2p.exceptions import MalformedMessage

from p2p.protocol import (
    Command,
    Protocol,
    _DecodedMsgType,
)

if TYPE_CHECKING:
    from p2p.peer import (  # noqa: F401
        BasePeer
    )


class Hello(Command):
    _cmd_id = 0
    decode_strict = False
    structure = [
        ('version', sedes.big_endian_int),
        ('client_version_string', sedes.text),
        ('capabilities', sedes.CountableList(sedes.List([sedes.text, sedes.big_endian_int]))),
        ('listen_port', sedes.big_endian_int),
        ('remote_pubkey', sedes.binary)
    ]

    @classmethod
    def decompress_payload(cls, raw_payload: bytes) -> bytes:
        # The `Hello` command doesn't support snappy compression
        return raw_payload

    @classmethod
    def compress_payload(cls, raw_payload: bytes) -> bytes:
        # The `Hello` command doesn't support snappy compression
        return raw_payload


@enum.unique
class DisconnectReason(enum.Enum):
    """More details at https://github.com/ethereum/wiki/wiki/%C3%90%CE%9EVp2p-Wire-Protocol#p2p"""
    disconnect_requested = 0
    tcp_sub_system_error = 1
    bad_protocol = 2
    useless_peer = 3
    too_many_peers = 4
    already_connected = 5
    incompatible_p2p_version = 6
    null_node_identity_received = 7
    client_quitting = 8
    unexpected_identity = 9
    connected_to_self = 10
    timeout = 11
    subprotocol_error = 16


class Disconnect(Command):
    _cmd_id = 1
    structure = [('reason', sedes.big_endian_int)]

    @classmethod
    def get_reason_name(cls, reason_id: int) -> str:
        try:
            return DisconnectReason(reason_id).name
        except ValueError:
            return "unknown reason"

    @classmethod
    def decode(cls, data: bytes) -> _DecodedMsgType:
        try:
            raw_decoded = cast(Dict[str, int], super().decode(data))
        except rlp.exceptions.ListDeserializationError:
            self.logger.warning("Malformed Disconnect message: %s", data)
            raise MalformedMessage(f"Malformed Disconnect message: {data}")
        return assoc(raw_decoded, 'reason_name', cls.get_reason_name(raw_decoded['reason']))


class Ping(Command):
    _cmd_id = 2


class Pong(Command):
    _cmd_id = 3


class P2PProtocol(Protocol):
    name = 'p2p'
    version = 5
    _commands = [Hello, Ping, Pong, Disconnect]
    cmd_length = 16

    def __init__(self, peer: 'BasePeer') -> None:
        # DEVp2p command ID offset is always 0, since it's the lowest-level protocol.
        # DEVp2p sessions always start with compression disabled, upgrading if remote supports it.
        super().__init__(peer, cmd_id_offset=0, snappy_support=False)

    def send_handshake(self) -> None:
        # TODO: move import out once this is in the trinity codebase
        from trinity._utils.version import construct_trinity_client_identifier
        data = dict(version=self.version,
                    client_version_string=construct_trinity_client_identifier(),
                    capabilities=self.peer.capabilities,
                    listen_port=self.peer.listen_port,
                    remote_pubkey=self.peer.privkey.public_key.to_bytes())
        header, body = self.cmd_by_type[Hello].encode(data)
        self.send(header, body)

    def send_disconnect(self, reason: DisconnectReason) -> None:
        msg: Dict[str, Any] = {"reason": reason}
        header, body = self.cmd_by_type[Disconnect].encode(msg)
        self.send(header, body)

    def send_pong(self) -> None:
        header, body = self.cmd_by_type[Pong].encode({})
        self.send(header, body)
