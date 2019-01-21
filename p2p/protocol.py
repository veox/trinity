from abc import ABC
import logging
import struct
from typing import (
    Any,
    Dict,
    Generic,
    List,
    Tuple,
    Type,
    TypeVar,
    TYPE_CHECKING,
    Union,
)

from mypy_extensions import (
    TypedDict,
)

import snappy

import rlp
from rlp import sedes

from eth.constants import NULL_BYTE

from p2p.exceptions import (
    MalformedMessage,
)
from p2p._utils import get_devp2p_cmd_id

# Workaround for import cycles caused by type annotations:
# http://mypy.readthedocs.io/en/latest/common_issues.html#import-cycles
if TYPE_CHECKING:
    from p2p.peer import BasePeer  # noqa: F401


class TypedDictPayload(TypedDict):
    pass


PayloadType = Union[
    Dict[str, Any],
    List[rlp.Serializable],
    Tuple[rlp.Serializable, ...],
    TypedDictPayload,
]

# A payload to be delivered with a request
TRequestPayload = TypeVar('TRequestPayload', bound=PayloadType, covariant=True)

# for backwards compatibility for internal references in p2p:
_DecodedMsgType = PayloadType


class Command:
    _cmd_id: int = None
    _cmd_id_offset: int = None
    cmd_id = None
    _snappy_support = None

    decode_strict = True
    structure: List[Tuple[str, Any]] = []

    _logger: logging.Logger = None

    # FIXME: does this even work?
    @property
    def logger(cls) -> logging.Logger:
        if cls._logger is None:
            cls._logger = logging.getLogger(f"p2p.protocol.{type(cls).__name__}")
        return cls._logger

    # FIXME: make @classmethod
    @property
    def is_base_protocol(cls) -> bool:
        return cls._cmd_id_offset == 0

    # FIXME: reference to `self`
    # def __str__(self) -> str:
    #     return f"{type(self).__name__} (cmd_id={cls.cmd_id})"

    @classmethod
    def encode_payload(cls, data: Union[PayloadType, sedes.CountableList]) -> bytes:
        if isinstance(data, dict):  # convert dict to ordered list
            if not isinstance(cls.structure, list):
                raise ValueError("Command.structure must be a list when data is a dict")
            expected_keys = sorted(name for name, _ in cls.structure)
            data_keys = sorted(data.keys())
            if data_keys != expected_keys:
                raise ValueError(
                    f"Keys in data dict ({data_keys}) do not match expected keys ({expected_keys})"
                )
            data = [data[name] for name, _ in cls.structure]
        if isinstance(cls.structure, sedes.CountableList):
            encoder = cls.structure
        else:
            encoder = sedes.List([type_ for _, type_ in cls.structure])
        return rlp.encode(data, sedes=encoder)

    @classmethod
    def decode_payload(cls, rlp_data: bytes) -> PayloadType:
        if isinstance(cls.structure, sedes.CountableList):
            decoder = cls.structure
        else:
            decoder = sedes.List(
                [type_ for _, type_ in cls.structure], strict=cls.decode_strict)
        try:
            data = rlp.decode(rlp_data, sedes=decoder, recursive_cache=True)
        except rlp.DecodingError as err:
            raise MalformedMessage(f"Malformed {type(cls).__name__} message: {err!r}") from err

        if isinstance(cls.structure, sedes.CountableList):
            return data
        return {
            field_name: value
            for ((field_name, _), value)
            in zip(cls.structure, data)
        }

    @classmethod
    def decode(cls, data: bytes) -> PayloadType:
        packet_type = get_devp2p_cmd_id(data)
        if packet_type != cls.cmd_id:
            raise MalformedMessage(f"Wrong packet type: {packet_type}, expected {cls.cmd_id}")

        compressed_payload = data[1:]
        encoded_payload = cls.decompress_payload(compressed_payload)

        return cls.decode_payload(encoded_payload)

    @classmethod
    def decompress_payload(cls, raw_payload: bytes) -> bytes:
        # Do the Snappy Decompression only if Snappy Compression is supported by the protocol
        if cls._snappy_support:
            return snappy.decompress(raw_payload)
        else:
            return raw_payload

    @classmethod
    def compress_payload(cls, raw_payload: bytes) -> bytes:
        # Do the Snappy Compression only if Snappy Compression is supported by the protocol
        if cls._snappy_support:
            return snappy.compress(raw_payload)
        else:
            return raw_payload

    @classmethod
    def encode(cls, data: PayloadType) -> Tuple[bytes, bytes]:
        encoded_payload = cls.encode_payload(data)
        compressed_payload = cls.compress_payload(encoded_payload)

        enc_cmd_id = rlp.encode(cls.cmd_id, sedes=rlp.sedes.big_endian_int)
        frame_size = len(enc_cmd_id) + len(compressed_payload)
        if frame_size.bit_length() > 24:
            raise ValueError("Frame size has to fit in a 3-byte integer")

        # Drop the first byte as, per the spec, frame_size must be a 3-byte int.
        header = struct.pack('>I', frame_size)[1:]
        # All clients seem to ignore frame header data, so we do the same, although I'm not sure
        # why geth uses the following value:
        # https://github.com/ethereum/go-ethereum/blob/master/p2p/rlpx.go#L556
        zero_header = b'\xc2\x80\x80'
        header += zero_header
        header = _pad_to_16_byte_boundary(header)

        body = _pad_to_16_byte_boundary(enc_cmd_id + compressed_payload)
        return header, body

### FIXME: move to p2p._utils module?..
# FIXME: module-level, global "look-up table"; is this acceptable?..
command_classes: Dict[Tuple, Type[Command]] = {}
# FIXME: use kwargs for feature specification; they shouldn't be spelled out here!
def get_command_class(cmd_class, cmd_id_offset, snappy_support):
    # TODO: use NamedTuple?..
    specifier = (cmd_class, cmd_id_offset, snappy_support)

    # use existing if available
    if specifier in command_classes.keys():
        return command_classes[specifier]

    class CommandClassInstance(cmd_class):
        _cmd_id_offset = cmd_id_offset
        _snappy_support = snappy_support

        cmd_id = _cmd_id_offset + cmd_class._cmd_id
        cmd_type = cmd_class

        # FIXME: `self`
        def __repr__(self):
            # FIXME: not, strictly speaking, correct (it's not a tuple!)
            return f'{specifier}'
        # def __type__(self):
        #     return cmd_class

    c = CommandClassInstance()
    command_classes[specifier] = c
    return c


class BaseRequest(ABC, Generic[TRequestPayload]):
    """
    Must define command_payload during init. This is the data that will
    be sent to the peer with the request command.
    """
    # Defined at init time, with specific parameters:
    command_payload: TRequestPayload

    # Defined as class attributes in subclasses
    # outbound command type
    cmd_type: Type[Command]
    # response command type
    response_type: Type[Command]


class Protocol:
    peer: 'BasePeer'
    name: str = None
    version: int = None
    cmd_length: int = None
    # List of "featureless" Command classes that this Protocol object instance supports.
    _commands: List[Type[Command]] = []

    _logger: logging.Logger = None

    def __init__(self, peer: 'BasePeer', cmd_id_offset: int, snappy_support: bool) -> None:
        self.peer = peer
        self._cmd_id_offset = cmd_id_offset
        self._snappy_support = snappy_support
        self._update_protocol_commands()

    def _update_protocol_commands(self) -> None:
        self.commands = [get_command_class(cmd_class, self._cmd_id_offset, self._snappy_support)
                         for cmd_class in self._commands]
        self.cmd_by_type = {cmd.cmd_type: cmd for cmd in self.commands}
        self.cmd_by_id = {cmd.cmd_id: cmd for cmd in self.commands}

    @property
    def logger(self) -> logging.Logger:
        if self._logger is None:
            self._logger = logging.getLogger(f"p2p.protocol.{type(self).__name__}")
        return self._logger

    @property
    def cmd_id_offset(self) -> int:
        '''TODO'''
        return self._cmd_id_offset

    @property
    def snappy_support(self) -> bool:
        '''TODO'''
        return self._snappy_support

    @snappy_support.setter
    def snappy_support(self, enabled: bool) -> None:
        '''TODO'''
        self._snappy_support = enabled
        self._update_protocol_commands()

    def send(self, header: bytes, body: bytes) -> None:
        self.peer.send(header, body)

    def send_request(self, request: BaseRequest[PayloadType]) -> None:
        command = self.cmd_by_type[request.cmd_type]
        header, body = command.encode(request.command_payload)
        self.send(header, body)

    def supports_command(self, cmd_type: Type[Command]) -> bool:
        return cmd_type in self._commands

    def __repr__(self) -> str:
        return "(%s, %d)" % (self.name, self.version)


def _pad_to_16_byte_boundary(data: bytes) -> bytes:
    """Pad the given data with NULL_BYTE up to the next 16-byte boundary."""
    remainder = len(data) % 16
    if remainder != 0:
        data += NULL_BYTE * (16 - remainder)
    return data
