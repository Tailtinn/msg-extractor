from __future__ import annotations


__all__ = [
    'ClipboardFormatOrAnsiString',
    'OLEPresentationStream',
]


from typing import List, Optional, Union

from .. import constants
from ._helpers import BytesReader
from ..enums import ADVF, ClipboardFormat, DVAspect


class ClipboardFormatOrAnsiString:
    def __init__(self, reader : Union[bytes, BytesReader]):
        if isinstance(reader, bytes):
            reader = BytesReader(reader)

        self.__markerOrLength = reader.readUnsignedInt()
        if self.__markerOrLength > 0xFFFFFFFD:
            self.__ansiString = None
            self.__clipboardFormat = ClipboardFormat(reader.readUnsignedInt())
        elif self.__markerOrLength > 0:
            self.__ansiString = reader.read(self.__markerOrLength)
            self.__clipboardFormat = None
        else:
            self.__ansiString = None
            self.__clipboardFormat = None

    def toBytes(self) -> bytes:
        ret = constants.st.ST_LE_UI32.pack(self.markerOrLength)
        if self.markerOrLength > 0xFFFFFFFD:
            ret += constants.st.ST_LE_UI32.pack(self.clipboardFormat)
        elif self.markerOrLength > 0:
            ret += self.ansiString
        return ret

    @property
    def ansiString(self) -> Optional[bytes]:
        """
        The null-terminated ANSI string, as bytes, of the name of a registered
        clipboard format. Only set if markerOrLength is not 0x00000000,
        0xFFFFFFFE, or 0xFFFFFFFF.

        Setting this will modify the markerOrLength field automatically.
        """
        return self.__ansiString

    @ansiString.setter
    def setter(self, val : bytes) -> None:
        if not val:
            raise ValueError('Cannot set :property ansiString: to None or empty bytes. Set :property markerOrLength: to a value ')

        self.__ansiString = val

    @property
    def clipboardFormat(self) -> Optional[ClipboardFormat]:
        """
        The clipboard format, if any.

        To set this, make sure that :property markerOrLength: is 0xFFFFFFFE or
        0xFFFFFFFF *before* setting.
        """
        return self.__clipboardFormat

    @clipboardFormat.setter
    def setter(self, val : ClipboardFormat) -> None:
        if not val:
            raise ValueError('Cannot set clipboard format to None.')
        if self.markerOrLength < 0xFFFFFFFE:
            raise ValueError('Cannot set the clipboard format while the marker or length is not 0xFFFFFFFE or 0xFFFFFFFF')
        self.__clipboardFormat = val

    @property
    def markerOrLength(self) -> int:
        """
        If set the 0x00000000, then neither the format property nor the
        ansiString property will be set. If it is 0xFFFFFFFF or 0xFFFFFFFE, then
        the clipboardFormat property will be set. Otherwise, the ansiString
        property
        will be set.
        """
        return self.__markerOrLength

    @markerOrLength.setter
    def setter(self, val : int) -> None:
        if val < 0:
            raise ValueError('markerOrLength must be a positive integer.')
        if val > 0xFFFFFFFF:
            raise ValueError('markerOrLength must be a 4 byte unsigned integer.')

        if val == 0:
            self.__ansiString = None
            self.__clipboardFormat = None
        elif val > 0xFFFFFFFD:
            self.__ansiString = None
            self.__clipboardFormat = ClipboardFormat.CF_BITMAP
        else:
            raise ValueError('Cannot set :property markerOrLength: to a length value. Set :property ansiString: instead.')
        self.__markerOrLength = val



class DVTargetDevice:
    pass # TODO



class OLEPresentationStream:
    """
    [MS-OLEDS] OLEPresentationStream.
    """
    ansiClipboardFormat : ClipboardFormatOrAnsiString
    targetDeviceSize : int
    targetDevice : Optional[DVTargetDevice]
    aspect : Union[int, DVAspect]
    lindex : int
    advf : Union[int, ADVF]
    width : int
    height : int
    data : int
    reserved2 : Optional[bytes]
    tocSignature : int
    tocEntries : List[TOCEntry]

    def __init__(self, data : bytes):
        reader = BytesReader(data)
        self.ansiClipboardFormat = ClipboardFormatOrAnsiString(reader)

        # Validate the structure based on the documentation.
        if self.ansiClipboardFormat.markerOrLength == 0:
            raise ValueError('Invalid OLEPresentationStream (MarkerOrLength is 0).')
        if self.ansiClipboardFormat.clipboardFormat is ClipboardFormat.CF_BITMAP:
            raise ValueError('Invalid OLEPresentationStream (Format is CF_BITMAP).')
        if 0x201 < self.ansiClipboardFormat.markerOrLength < 0xFFFFFFFE:
            raise ValueError('Invalid OLEPresentationStream (ANSI length was more than 0x201).')

        self.targetDeviceSize = reader.readUnsignedInt()
        if self.targetDevice < 0x4:
            raise ValueError('Invalid OLEPresentationStream (TargetDeviceSize was less than 4).')
        if self.targetDevice > 0x4:
            # Read the TargetDevice field.
            self.targetDevice = DVTargetDevice(reader)
        else:
            self.targetDevice = None

        self.aspect = reader.readUnsignedInt()
        self.lindex = reader.readUnsignedInt()
        self.advf = reader.readUnsignedInt()

        # Reserved1.
        reader.readUnsignedInt()

        self.width = reader.readUnsignedInt()
        self.height = reader.readUnsignedInt()
        size = reader.readUnsignedInt()
        self.data = reader.read(size)

        if self.ansiClipboardFormat.clipboardFormat is ClipboardFormat.CF_METAFILEPICT:
            self.reserved2 = reader.read(18)
        else:
            self.reserved2 = None

        self.tocSignature = reader.readUnsignedInt()
        self.tocEntries = []
        if self.tocSignature == 0x494E414E: # b'NANI' in little endian.
            for x in range(reader.readUnsignedInt()):
                self.tocEntries.append(TOCEntry(reader))



class TOCEntry:
    def __init__(self, reader : Union[bytes, BytesReader]):
        if isinstance(reader, bytes):
            reader = BytesReader(reader)

        # TODO