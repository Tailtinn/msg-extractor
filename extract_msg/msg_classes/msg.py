from __future__ import annotations


__all__ = [
    'MSGFile',
]


import codecs
import copy
import datetime
import functools
import io
import logging
import os
import pathlib
import weakref
import zipfile

import olefile

from typing import (
        Any, Callable, cast, Dict, List, Optional, Tuple, TypeVar, Union
    )

from .. import constants
from ..attachments import (
        AttachmentBase, initStandardAttachment, SignedAttachment
    )
from ..encoding import lookupCodePage
from ..enums import (
        ErrorBehavior, InsecureFeatures, Importance, Priority, PropertiesType,
        SaveType, Sensitivity, SideEffect
    )
from ..exceptions import (
        ConversionError, InvalidFileFormatError, PrefixError,
        StandardViolationError
    )
from ..properties.named import Named, NamedProperties
from ..properties.prop import FixedLengthProp
from ..properties.properties_store import PropertiesStore
from ..utils import (
        divide, hasLen, inputToMsgPath, makeWeakRef, msgPathToString,
        parseType, verifyPropertyId, verifyType, windowsUnicode
    )


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_T = TypeVar('_T')


class MSGFile:
    """
    Parser for .msg files.
    """

    def __init__(self, path, **kwargs):
        """
        :param path: path to the msg file in the system or is the raw msg file.
        :param prefix: Used for extracting embedded msg files inside the main
            one. Do not set manually unless you know what you are doing.
        :param parentMsg: Used for synchronizing instances of shared objects. Do
            not set this unless you know what you are doing.
        :param initAttachment: Optional, the method used when creating an
            attachment for an MSG file. MUST be a function that takes 2
            arguments (the MSGFile instance and the directory in the MSG file
            where the attachment is) and returns an instance of AttachmentBase.
        :param delayAttachments: Optional, delays the initialization of
            attachments until the user attempts to retrieve them. Allows MSG
            files with bad attachments to be initialized so the other data can
            be retrieved.
        :param filename: Optional, the filename to be used by default when
            saving.
        :param errorBehavior: Optional, the behavior to use in the event of
            certain types of errors. Uses the ErrorBehavior enum.
        :param overrideEncoding: Optional, an encoding to use instead of the one
            specified by the msg file. Do not report encoding errors caused by
            this.
        :param treePath: Internal variable used for giving representation of the
            path, as a tuple of objects, of the MSGFile. When passing, this is
            the path to the parent object of this instance.
        :param insecureFeatures: Optional, an enum value that specifies if
            certain insecure features should be enabled. These features should
            only be used on data that you trust. Uses the InsecureFeatures enum.

        :raises InvalidFileFormatError: If the file is not an OleFile or could
            not be parsed as an MSG file.
        :raises StandardViolationError: If some part of the file badly violates
            the standard.
        :raises IOError: If there is an issue opening the MSG file.
        :raises NameError: If the encoding provided is not supported.
        :raises PrefixError: If the prefix is not a supported type.
        :raises TypeError: If the parent is not an instance of MSGFile or a
            subclass.
        :raises ValueError: If the attachment error behavior is not valid.

        It's recommended to check the error message to ensure you know why a
        specific exceptions was raised.
        """
        # Retrieve all the kwargs that we need.
        self.__inscFeat = kwargs.get('insecureFeatures', InsecureFeatures.NONE)
        prefix = kwargs.get('prefix', '')
        self.__parentMsg = makeWeakRef(cast(MSGFile, kwargs.get('parentMsg')))
        self.__treePath = kwargs.get('treePath', []) + [makeWeakRef(self)]
        # Verify it is a valid class.
        if self.__parentMsg and not isinstance(self.__parentMsg(), MSGFile):
            raise TypeError(':param parentMsg: must be an instance of MSGFile or a subclass.')
        filename = kwargs.get('filename', None)
        overrideEncoding = kwargs.get('overrideEncoding', None)

        # WARNING DO NOT MANUALLY MODIFY PREFIX. Let the program set it.
        self.__path = path
        self.__initAttachmentFunc = kwargs.get('initAttachment', initStandardAttachment)
        self.__attachmentsDelayed = kwargs.get('delayAttachments', False)
        self.__attachmentsReady = False
        self.__errorBehavior = ErrorBehavior(kwargs.get('errorBehavior', ErrorBehavior.THROW))

        if overrideEncoding is not None:
            codecs.lookup(overrideEncoding)
            logger.warning('You have chosen to override the string encoding. Do not report encoding errors caused by this.')
            self.__stringEncoding = overrideEncoding
        self.__overrideEncoding = overrideEncoding

        self.__listDirRes = {}

        if self.__parentMsg:
            # We should be able to directly access the private variables of
            # another instance with no issue.
            self.__ole = self.__parentMsg().__ole
            self.__oleOwner = False
        else:
            # Verify the path at least evaluates to True, as not doing so can
            # allow an OleFile to be created without a path.
            if not path:
                raise ValueError(':param path: must be set and must not be empty.')
            try:
                if ErrorBehavior.OLE_DEFECT_INCORRECT in self.errorBehavior:
                    defect = olefile.DEFECT_FATAL
                else:
                    defect = olefile.DEFECT_INCORRECT
                self.__ole = olefile.OleFileIO(path, raise_defects = defect)
            except OSError as e:
                logger.error(e)
                if str(e) == 'not an OLE2 structured storage file':
                    raise InvalidFileFormatError(e)
                else:
                    raise
            # This is a variable that tells whether we own the olefile. Used for
            # closing. We set it here for error handling.
            self.__oleOwner = True

        # The rest *must* be in a try-except block to ensure we close the file.
        try:
            kwargsCopy = copy.copy(kwargs)
            if 'prefix' in kwargsCopy:
                del kwargsCopy['prefix']
            if 'parentMsg' in kwargsCopy:
                del kwargsCopy['parentMsg']
            if 'filename' in kwargsCopy:
                del kwargsCopy['filename']
            if 'treePath' in kwargsCopy:
                del kwargsCopy['treePath']
            self.__kwargs = kwargsCopy

            prefixl = []
            if prefix:
                try:
                    prefixl = inputToMsgPath(prefix)
                    prefix = '/'.join(prefixl) + '/'
                except ConversionError:
                    raise PrefixError(f'The provided prefix could not be used: {prefix}')
            self.__prefix = prefix
            self.__prefixList = prefixl
            self.__prefixLen = len(prefixl)
            if prefix and not filename:
                filename = self.getStringStream(prefixl[:-1] + ['__substg1.0_3001'], prefix = False)
            if filename:
                self.filename = filename
            elif hasLen(path):
                if len(path) < 1536:
                    self.filename = str(path)
                else:
                    self.filename = None
            elif isinstance(path, pathlib.Path):
                self.filename = str(path)
            else:
                self.filename = None

            self.__open = True

            # Now, load the attachments if we are not delaying them.
            if not self.__attachmentsDelayed:
                self.attachments
        except:
            # *Any* exception here requires that we close the file.
            try:
                self.close()
            except:
                pass
            # Raise the exception after trying to close the file.
            raise

    def __enter__(self) -> MSGFile:
        self.__ole.__enter__()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _getOleEntry(self, filename, prefix : bool = True) -> olefile.olefile.OleDirectoryEntry:
        """
        Finds the directory entry from the olefile for the stream or storage
        specified. Use '/' to get the root entry.
        """
        sid = -1
        if filename == '/':
            if prefix and self.__prefix:
                sid = self.__ole._find(self.__prefixList)
            else:
                return self.__ole.direntries[0]
        else:
            sid = self.__ole._find(self.fixPath(filename, prefix))

        return self.__ole.direntries[sid]

    def _getStream(self, filename, prefix : bool = True) -> Optional[bytes]:
        """
        Gets a binary representation of the requested filename.

        This should ALWAYS return a bytes object if it was found, otherwise
        returns None.
        """
        import warnings
        warnings.warn(':method _getStream: has been deprecated and moved to the public api. Use :method getStream: instead (remove the underscore).', DeprecationWarning)
        return self.getStream(filename, prefix)

    def _getStringStream(self, filename, prefix : bool = True) -> Optional[str]:
        """
        Gets a string representation of the requested filename.

        Rather than the full filename, you should only feed this function the
        filename sans the type. So if the full name is "__substg1.0_001A001F",
        the filename this function should receive should be "__substg1.0_001A".

        This should ALWAYS return a string if it was found, otherwise returns
        None.
        """
        import warnings
        warnings.warn(':method _getStringStream: has been deprecated and moved to the public api. Use :method getStringStream: instead (remove the underscore).', DeprecationWarning)
        return self.getStringStream(filename, prefix)

    def _getTypedAs(self, _id : str, overrideClass = None, preserveNone : bool = True):
        """
        Like the other get as functions, but designed for when something
        could be multiple types (where only one will be present). This way you
        have no need to set the type, it will be handled for you.

        :param overrideClass: Class/function to use to morph the data that was
            read. The data will be the first argument to the class's __init__
            function or the function itself, if that is what is provided. By
            default, this will be completely ignored if the value was not found.
        :param preserveNone: If true (default), causes the function to ignore
            :param overrideClass: when the value could not be found (is None).
            If this is changed to False, then the value will be used regardless.
        """
        value = self._getTypedData(_id)
        # Check if we should be overriding the data type for this instance.
        if overrideClass is not None:
            if value is not None or not preserveNone:
                value = overrideClass(value)

        return value

    def _getTypedData(self, _id : str, _type = None, prefix : bool = True):
        """
        Gets the data for the specified id as the type that it is supposed to
        be. :param id: MUST be a 4 digit hexadecimal string.

        If you know for sure what type the data is before hand, you can specify
        it as being one of the strings in the constant FIXED_LENGTH_PROPS_STRING
        or VARIABLE_LENGTH_PROPS_STRING.
        """
        verifyPropertyId(_id)
        _id = _id.upper()
        found, result = self._getTypedStream('__substg1.0_' + _id, prefix, _type)
        if found:
            return result
        else:
            found, result = self._getTypedProperty(_id, _type)
            return result if found else None

    def _getTypedProperty(self, propertyID : str, _type = None) -> Tuple[bool, Optional[Any]]:
        """
        Gets the property with the specified id as the type that it is supposed
        to be. :param id: MUST be a 4 digit hexadecimal string.

        If you know for sure what type the property is before hand, you can
        specify it as being one of the strings in the constant
        FIXED_LENGTH_PROPS_STRING or VARIABLE_LENGTH_PROPS_STRING.
        """
        verifyPropertyId(propertyID)
        if _type:
            verifyType(_type)
            propertyID += _type

        notFound = object()
        ret = self.getPropertyVal(propertyID, notFound)
        if ret is notFound:
            return False, None

        return True, ret

    def _getTypedStream(self, filename, prefix : bool = True, _type = None) -> Tuple[bool, Optional[Any]]:
        """
        Gets the contents of the specified stream as the type that it is
        supposed to be.

        Rather than the full filename, you should only feed this function the
        filename sans the type. So if the full name is "__substg1.0_001A001F",
        the filename this function should receive should be "__substg1.0_001A".

        If you know for sure what type the stream is before hand, you can
        specify it as being one of the strings in the constant
        FIXED_LENGTH_PROPS_STRING or VARIABLE_LENGTH_PROPS_STRING.

        If you have not specified the type, the type this function returns in
        many cases cannot be predicted. As such, when using this function it is
        best for you to check the type that it returns. If the function returns
        None, that means it could not find the stream specified.
        """
        verifyType(_type)
        filename = self.fixPath(filename, prefix)
        for x in (filename + _type,) if _type is not None else self.slistDir():
            if x.startswith(filename) and '-' not in x:
                if (contents := self.getStream(x, False)) is None:
                    continue
                if len(contents) == 0:
                    return True, None # We found the file, but it was empty.
                extras = []
                _type = x[-4:]
                if x[-4] == '1': # It's a multiple
                    if _type in ('101F', '101E'):
                        streams = len(contents) // 4 # These lengths are normal.
                    elif _type == '1102':
                        streams = len(contents) // 8 # These lengths have 4 0x00 bytes at the end for seemingly no reason. They are "reserved" bytes
                    elif _type in ('1002', '1003', '1004', '1005', '1007', '1014', '1040', '1048'):
                        try:
                            streams = self.props[x[-8:]].realLength
                        except Exception:
                            logger.error(f'Could not find matching VariableLengthProp for stream {x}')
                            streams = len(contents) // (2 if _type in constants.MULTIPLE_2_BYTES else 4 if _type in constants.MULTIPLE_4_BYTES else 8 if _type in constants.MULTIPLE_8_BYTES else 16)
                    else:
                        raise NotImplementedError(f'The stream specified is of type {_type}. We don\'t currently understand exactly how this type works. If it is mandatory that you have the contents of this stream, please create an issue labled "NotImplementedError: _getTypedStream {_type}".')
                    if _type in ('101F', '101E', '1102'):
                        if self.exists(x + '-00000000', False):
                            for y in range(streams):
                                if self.exists((name := f'{x}-{y:08X}'), False):
                                    extras.append(self.getStream(name, False))
                    elif _type in ('1002', '1003', '1004', '1005', '1007', '1014', '1040', '1048'):
                        extras = divide(contents, (2 if _type in constants.MULTIPLE_2_BYTES else 4 if _type in constants.MULTIPLE_4_BYTES else 8 if _type in constants.MULTIPLE_8_BYTES else 16))
                        contents = streams
                return True, parseType(int(_type, 16), contents, self.stringEncoding, extras)
        return False, None # We didn't find the stream.

    def _oleListDir(self, streams : bool = True, storages : bool = False) -> List[List[str]]:
        """
        Calls :method OleFileIO.listdir: from the OleFileIO instance associated
        with this MSG file. Useful for if you need access to all the top level
        streams if this is an embedded MSG file.

        Returns a list of the streams and or storages depending on the arguments
        given.
        """
        return self.__ole.listdir(streams, storages)

    def close(self) -> None:
        if self.__open:
            if self.attachmentsReady:
                for attachment in self.attachments:
                    if attachment.type == 'msg':
                        attachment.data.close()

            if self.__oleOwner:
                self.__ole.close()

            self.__open = False

    def debug(self) -> None:
        for dir_ in self.listDir():
            if dir_[-1].endswith('001E') or dir_[-1].endswith('001F'):
                print('Directory: ' + str(dir_[:-1]))
                print(f'Contents: {self.getStream(dir_)}')

    def exists(self, inp, prefix : bool = True) -> bool:
        """
        Checks if :param inp: exists in the msg file.
        """
        inp = self.fixPath(inp, prefix)
        return self.__ole.exists(inp)

    def sExists(self, inp, prefix : bool = True) -> bool:
        """
        Checks if string stream :param inp: exists in the msg file.
        """
        inp = self.fixPath(inp, prefix)
        return self.exists(inp + '001F') or self.exists(inp + '001E')

    def existsTypedProperty(self, _id, location = None, _type = None, prefix = True, propertiesInstance = None):
        """
        Determines if the stream with the provided id exists in the location
        specified. If no location is specified, the root directory is searched.
        The return of this function is 2 values, the first being a boolean for
        if anything was found, and the second being how many were found.

        Because of how this function works, any folder that contains it's own
        "__properties_version1.0" file should have this function called from
        it's class.
        """
        verifyPropertyId(_id)
        verifyType(_type)
        _id = _id.upper()
        if propertiesInstance is None:
            propertiesInstance = self.props
        prefixList = self.prefixList if prefix else []
        if location is not None:
            prefixList.append(location)
        prefixList = inputToMsgPath(prefixList)
        usableId = _id + _type if _type else _id
        foundNumber = 0
        foundStreams = []
        for item in self.listDir():
            if len(item) > self.__prefixLen:
                if item[self.__prefixLen].startswith('__substg1.0_' + usableId) and item[self.__prefixLen] not in foundStreams:
                    foundNumber += 1
                    foundStreams.append(item[self.__prefixLen])
        for x in propertiesInstance:
            if x.startswith(usableId):
                for y in foundStreams:
                    if y.endswith(x):
                        break
                else:
                    foundNumber += 1
        return (foundNumber > 0), foundNumber

    def export(self, path) -> None:
        """
        Exports the contents of this MSG file to a new MSG files specified by
        the path given. If this is an embedded MSG file, the embedded streams
        and directories will be added to it as if they were at the root,
        allowing you to save it as it's own MSG file.

        :param path: An IO device with a write method which accepts bytes or a
            path-like object (including strings and pathlib.Path objects).
        """
        from ..ole_writer import OleWriter

        # Create an instance of the class used for writing a new OLE file.
        writer = OleWriter()
        # Add all file and directory entries to it. If this
        writer.fromMsg(self)
        writer.write(path)

    def exportBytes(self) -> bytes:
        """
        Saves a new copy of the MSG file, returning the bytes.
        """
        out = io.BytesIO()
        self.export(out)
        return out.getvalue()

    def fixPath(self, inp, prefix : bool = True) -> str:
        """
        Changes paths so that they have the proper prefix (should :param prefix:
        be True) and are strings rather than lists or tuples.
        """
        inp = msgPathToString(inp)
        if prefix:
            inp = self.__prefix + inp
        return inp

    def getMultipleBinary(self, filename, prefix : bool = True) -> Optional[List[bytes]]:
        """
        Gets a multiple binary property as a list of bytes objects.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_00011102" then the
        filename would simply be "__substg1.0_0001".

        :param prefix: Bool, whether to search for the entry at the root of the
            MSG file (False) or look in the current child MSG file (True).
        """
        filename = self.fixPath(filename, prefix) + '1102'
        multStream = self.getStream(filename)
        if multStream is None:
            return None

        if len(multStream) == 0:
            return []
        elif len(multStream) & 7 != 0:
            raise StandardViolationError(f'Length stream for multiple binary was not a multiple of 8.')
        else:
            ret = [self.getStream(filename + f'-{x:08X}') for x in range(len(multStream) // 8)]
            # We could do more checking here, but we'll just check for None.
            if (index := next((x for x in ret if x is None), -1)) != -1:
                logger.error('Unable to get the desired number of binary streams for multiple, not all streams were found.')
                return ret[:index]
            return ret

    def getMultipleString(self, filename, prefix : bool = True) -> Optional[List[str]]:
        """
        Gets a multiple string property as a list of str objects.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_00011102" then the
        filename would simply be "__substg1.0_0001".

        :param prefix: Bool, whether to search for the entry at the root of the
            MSG file (False) or look in the current child MSG file (True).
        """
        filename = self.fixPath(filename, prefix) + '101F' if self.areStringsUnicode else '101E'
        multStream = self.getStream(filename)
        if multStream is None:
            return []

        if len(multStream) == 0:
            return []
        elif len(multStream) & 3 != 0:
            raise StandardViolationError(f'Length stream for multiple string was not a multiple of 4.')
        else:
            ret = [self.getStream(filename + f'-{x:08X}') for x in range(len(multStream) // 4)]
            # We could do more checking here, but we'll just check for None.
            for index, item in enumerate(ret):
                if item is None:
                    logger.error('Unable to get the desired number of string streams for multiple, not all streams were found.')
                    return ret[:index]
                # Decode the bytes and remove the null byte.
                ret[index] = item.decode(self.stringEncoding)[:-1]
            return ret

    def getNamedAs(self, propertyName : str, guid : str, overrideClass : Callable[..., _T]) -> Optional[_T]:
        """
        Returns the named property, setting the class if specified.

        :param overrideClass: Class/function to use to morph the data that was
            read. The data will be the first argument to the class's __init__
            function or the function itself, if that is what is provided. If
            the value is None, this function is not called. If you want it to
            be called regardless, you should handle the data directly.
        """
        value = self.getNamedProp(propertyName, guid)
        if value is not None:
            value = overrideClass(value)
        return value

    def getNamedProp(self, propertyName : str, guid : str, default : _T = None) -> Union[Any, _T]:
        """
        instance.namedProperties.get((propertyName, guid), default)

        Can be override to create new behavior.
        """
        return self.namedProperties.get((propertyName, guid), default)

    def getPropertyAs(self, propertyName, overrideClass : Callable[..., _T]) -> Optional[_T]:
        """
        Returns the property, setting the class if found.

        :param overrideClass: Class/function to use to morph the data that was
            read. The data will be the first argument to the class's __init__
            function or the function itself, if that is what is provided. If
            the value is None, this function is not called. If you want it to
            be called regardless, you should handle the data directly.
        """
        value = self.getPropertyVal(propertyName)

        if value is not None:
            value = overrideClass(value)

        return value

    def getPropertyVal(self, name, default : _T = None) -> Union[Any, _T]:
        """
        instance.props.getValue(name, default)

        Can be overriden to create new behavior.
        """
        return self.props.getValue(name, default)

    def getSingleOrMultipleBinary(self, filename, prefix : bool = True) -> Optional[Union[List[bytes], bytes]]:
        """
        A combination of :method getStringStream: and
        :method getMultipleString:.

        Checks to see if a single binary stream exists to return, otherwise
        tries to return the multiple binary stream of the same ID.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_00010102" then the
        filename would simply be "__substg1.0_0001".
        """
        filename = self.fixPath(filename, prefix)
        # Check for a single binary stream first.
        if (ret := self.getStream(filename + '0102', False)) is not None:
            return ret
        # Otherwise, we just let the return from `getMultipleBinary` do the
        # work.
        return self.getMultipleBinary(filename, False)

    def getSingleOrMultipleString(self, filename, prefix : bool = True) -> Optional[Union[List[str], str]]:
        """
        A combination of :method getStringStream: and
        :method getMultipleString:.

        Checks to see if a single string stream exists to return, otherwise
        tries to return the multiple string stream of the same ID.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_0001001F" then the
        filename would simply be "__substg1.0_0001".
        """
        filename = self.fixPath(filename, prefix)
        # Check for a single stribng stream first.
        if (ret := self.getStringStream(filename, False)) is not None:
            return ret
        # Otherwise, we just let the return from `getMultipleString` do the
        # work.
        return self.getMultipleString(filename, False)

    def getStream(self, filename, prefix : bool = True) -> Optional[bytes]:
        """
        Gets a binary representation of the requested filename.

        This should ALWAYS return a bytes object if it was found, otherwise
        returns None.

        :param prefix: Bool, whether to search for the entry at the root of the
            MSG file (False) or look in the current child MSG file (True).
        """
        filename = self.fixPath(filename, prefix)
        if self.exists(filename, False):
            with self.__ole.openstream(filename) as stream:
                return stream.read() or b''
        else:
            logger.info(f'Stream "{filename}" was requested but could not be found. Returning `None`.')
            return None

    def getStreamAs(self, streamID, overrideClass : Callable[..., _T]) -> Optional[_T]:
        """
        Returns the specified stream, modifying it to the specified class if it
        is found.

        :param overrideClass: Class/function to use to morph the data that was
            read. The data will be the first argument to the class's __init__
            function or the function itself, if that is what is provided. If
            the value is None, this function is not called. If you want it to
            be called regardless, you should handle the data directly.
        """
        value = self.getStream(streamID)

        if value is not None:
            value = overrideClass(value)

        return value

    def getStringStream(self, filename, prefix : bool = True) -> Optional[str]:
        """
        Gets a string representation of the requested filename.

        Rather than the full filename, you should only feed this function the
        filename sans the type. So if the full name is "__substg1.0_001A001F",
        the filename this function should receive should be "__substg1.0_001A".

        This should ALWAYS return a string if it was found, otherwise returns
        None.

        :param prefix: Bool, whether to search for the entry at the root of the
            MSG file (False) or look in the current child MSG file (True).
        """
        filename = self.fixPath(filename, prefix)
        if self.areStringsUnicode:
            return windowsUnicode(self.getStream(filename + '001F', prefix = False))
        else:
            tmp = self.getStream(filename + '001E', prefix = False)
            return None if tmp is None else tmp.decode(self.stringEncoding)

    def getStringStreamAs(self, streamID, overrideClass : Callable[..., _T]) -> Optional[_T]:
        """
        Returns the specified string stream, modifying it to the specified
        class if it is found.

        :param overrideClass: Class/function to use to morph the data that was
            read. The data will be the first argument to the class's __init__
            function or the function itself, if that is what is provided. If
            the value is None, this function is not called. If you want it to
            be called regardless, you should handle the data directly.
        """
        value = self.getStream(streamID)

        if value is not None:
            value = overrideClass(value)

        return value

    def listDir(self, streams : bool = True, storages : bool = False, includePrefix : bool = True) -> List[List[str]]:
        """
        Replacement for OleFileIO.listdir that runs at the current prefix
        directory.

        :param includePrefix: If false, removed the part of the path that is the
            prefix.
        """
        # Get the items from OleFileIO.
        try:
            return self.__listDirRes[(streams, storages, includePrefix)]
        except KeyError:
            entries = self.__ole.listdir(streams, storages)
            if not self.__prefix:
                return entries
            prefix = self.__prefix.split('/')
            if prefix[-1] == '':
                prefix.pop()

            prefixLength = self.__prefixLen
            entries = [x for x in entries if len(x) > prefixLength and x[:prefixLength] == prefix]
            if not includePrefix:
                entries = [x[prefixLength:] for x in entries]
            self.__listDirRes[(streams, storages, includePrefix)] = entries

            return entries

    def slistDir(self, streams : bool = True, storages : bool = False, includePrefix : bool = True) -> List[str]:
        """
        Replacement for OleFileIO.listdir that runs at the current prefix
        directory. Returns a list of strings instead of lists.
        """
        return [msgPathToString(x) for x in self.listDir(streams, storages, includePrefix)]

    def save(self, **kwargs) -> constants.SAVE_TYPE:
        if kwargs.get('skipNotImplemented', False):
            return (SaveType.NONE, None)

        raise NotImplementedError(f'Saving is not yet supported for the {self.__class__.__name__} class.')

    def saveAttachments(self, **kwargs) -> None:
        """
        Saves only attachments in the same folder.

        :params skipHidden: If True, skips attachments marked as hidden.
            (Default: False)
        """
        skipHidden = kwargs.get('skipHidden', False)
        for attachment in self.attachments:
            if not (skipHidden and attachment.hidden):
                attachment.save(**kwargs)

    def saveRaw(self, path) -> None:
        # Create a 'raw' folder.
        path = pathlib.Path(path)
        # Make the location.
        os.makedirs(path, exist_ok = True)
        # Create the zipfile.
        path /= 'raw.zip'
        if path.exists():
            raise FileExistsError(f'File "{path}" already exists.')
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zfile:
            # Loop through all the directories
            for dir_ in self.listDir():
                sysdir = '/'.join(dir_)
                code = dir_[-1][-8:]
                if constants.PROPERTIES.get(code):
                    sysdir += ' - ' + constants.PROPERTIES[code]

                # Generate appropriate filename.
                if dir_[-1].endswith('001E') or dir_[-1].endswith('001F'):
                    filename = 'contents.txt'
                else:
                    filename = 'contents.bin'

                # Save contents of directory.
                with zfile.open(sysdir + '/' + filename, 'w') as f:
                    data = self.getStream(dir_)
                    # Specifically check for None. If this is bytes we still want to do this line.
                    # There was actually this weird issue where for some reason data would be bytes
                    # but then also simultaneously register as None?
                    if data is not None:
                        f.write(data)

    @functools.cached_property
    def areStringsUnicode(self) -> bool:
        """
        Returns a boolean telling if the strings are unicode encoded.
        """
        return (self.getPropertyVal('340D0003', 0) & 0x40000) != 0

    @functools.cached_property
    def attachments(self) -> Union[List[AttachmentBase], List[SignedAttachment]]:
        """
        Returns a list of all attachments.
        """
        # Get the attachments.
        attachmentDirs = []
        for dir_ in self.listDir(False, True, False):
            if dir_[0].startswith('__attach') and dir_[0] not in attachmentDirs:
                attachmentDirs.append(dir_[0])

        attachments = []

        for attachmentDir in attachmentDirs:
            attachments.append(self.initAttachmentFunc(self, attachmentDir))

        self.__attachmentsReady = True

        return attachments

    @property
    def attachmentsDelayed(self) -> bool:
        """
        Returns True if the attachment initialization was delayed.
        """
        return self.__attachmentsDelayed

    @property
    def attachmentsReady(self) -> bool:
        """
        Returns True if the attachments are ready to be used.
        """
        return self.__attachmentsReady

    @functools.cached_property
    def classified(self) -> bool:
        """
        Indicates whether the contents of this message are regarded as
        classified information.
        """
        return bool(self.getNamedProp('85B5', constants.ps.PSETID_COMMON))

    @functools.cached_property
    def classType(self) -> Optional[str]:
        """
        The class type of the MSG file.
        """
        return self.getStringStream('__substg1.0_001A')

    @functools.cached_property
    def commonEnd(self) -> Optional[datetime.datetime]:
        """
        The end time for the object.
        """
        return self.getNamedProp('8517', constants.ps.PSETID_COMMON)

    @functools.cached_property
    def commonStart(self) -> Optional[datetime.datetime]:
        """
        The start time for the object.
        """
        return self.getNamedProp('8516', constants.ps.PSETID_COMMON)

    @functools.cached_property
    def currentVersion(self) -> Optional[int]:
        """
        Specifies the build number of the client application that sent the
        message.
        """
        return self.getNamedProp('8552', constants.ps.PSETID_COMMON)

    @functools.cached_property
    def currentVersionName(self) -> Optional[str]:
        """
        Specifies the name of the client application that sent the message.
        """
        return self.getNamedProp('8554', constants.ps.PSETID_COMMON)

    @property
    def errorBehavior(self) -> ErrorBehavior:
        """
        The behavior to follow when certain errors occur. Will be an instance of
        the ErrorBehavior enum.
        """
        return self.__errorBehavior

    @functools.cached_property
    def importance(self) -> Optional[Importance]:
        """
        The specified importance of the msg file.
        """
        return self.getPropertyAs('00170003', Importance)

    @property
    def importanceString(self) -> Union[str, None]:
        """
        Returns the string to use for saving. If the importance is medium then
        it returns None. Mainly used for saving.
        """
        return {
            Importance.HIGH: 'High',
            Importance.MEDIUM: None,
            Importance.LOW: 'Low',
            None: None,
        }[self.importance]

    @property
    def initAttachmentFunc(self) -> Callable[[MSGFile, str], AttachmentBase]:
        """
        Returns the method for initializing attachments being used, should you
        need to use it externally for whatever reason.
        """
        return self.__initAttachmentFunc

    @property
    def insecureFeatures(self) -> InsecureFeatures:
        """
        An enum specifying what insecure features have been enabled for this
        file.
        """
        return self.__inscFeat

    @property
    def kwargs(self) -> Dict[str, object]:
        """
        The kwargs used to initialize this message, excluding the prefix. This
        is used for initializing embedded msg files.
        """
        return self.__kwargs

    @functools.cached_property
    def named(self) -> Named:
        """
        The main named properties storage. This is not usable to access the data
        of the properties directly.

        :raises ReferenceError: The parent MSGFile instance has been garbage
            collected.
        """
        # Handle the parent msg file existing.
        if self.__parentMsg:
            # Try to get the named properties and use that for our main
            # instance.
            if (msg := self.__parentMsg()) is None:
                raise ReferenceError('Parent MSGFile instance has been garbage collected.')
            return msg.named
        else:
            return Named(self)

    @functools.cached_property
    def namedProperties(self) -> NamedProperties:
        """
        The NamedProperties instances usable to access the data for named
        properties.
        """
        return NamedProperties(self.named, self)

    @property
    def overrideEncoding(self):
        """
        Returns None is the encoding has not been overriden, otherwise returns
        the encoding.
        """
        return self.__overrideEncoding

    @property
    def path(self):
        """
        Returns the message path if generated from a file, otherwise returns the
        data used to generate the Message instance.
        """
        return self.__path

    @property
    def prefix(self):
        """
        Returns the prefix of the Message instance. Intended for developer use.
        """
        return self.__prefix

    @property
    def prefixLen(self) -> int:
        """
        Returns the number of elements in the prefix.
        """
        return self.__prefixLen

    @property
    def prefixList(self) -> List[str]:
        """
        Returns the prefix list of the Message instance. Intended for developer
        use.
        """
        return copy.deepcopy(self.__prefixList)

    @functools.cached_property
    def priority(self) -> Optional[Priority]:
        """
        The specified priority of the msg file.
        """
        return self.getPropertyAs('00260003', Priority)

    @functools.cached_property
    def props(self) -> PropertiesStore:
        """
        Returns the Properties instance used by the MSGFile instance.
        """
        if not (stream := self.getStream('__properties_version1.0')):
            if ErrorBehavior.STANDARDS_VIOLATION in self.__errorBehavior:
                logger.error('File does not contain a property stream.')
            else:
                # Raise the exception from None so we don't get all the "during
                # the handling of the above exception" stuff.
                raise StandardViolationError('File does not contain a property stream.') from None
        return PropertiesStore(stream,
                               PropertiesType.MESSAGE if self.prefix == '' else PropertiesType.MESSAGE_EMBED)

    @functools.cached_property
    def sensitivity(self) -> Optional[Sensitivity]:
        """
        The specified sensitivity of the msg file.
        """
        return self.getPropertyAs('00360003', Sensitivity)

    @functools.cached_property
    def sideEffects(self) -> Optional[SideEffect]:
        """
        Controls how a Message object is handled by the client in relation to
        certain user interface actions by the user, such as deleting a message.
        """
        return self.getNamedAs('8510', constants.ps.PSETID_COMMON, SideEffect)

    @property
    def stringEncoding(self):
        try:
            return self.__stringEncoding
        except AttributeError:
            # We need to calculate the encoding.
            # Let's first check if the encoding will be unicode:
            if self.areStringsUnicode:
                self.__stringEncoding = "utf-16-le"
                return self.__stringEncoding
            else:
                # Well, it's not unicode. Now we have to figure out what it IS.
                if '3FFD0003' not in self.props:
                    # If this property is not set by the client, we SHOULD set
                    # it to ISO-8859-15, but MAY set it to ISO-8859-1.
                    logger.warning('Encoding property not found. Defaulting to ISO-8859-15.')
                    self.__stringEncoding = 'iso-8859-15'
                else:
                    enc = cast(int, self.getPropertyVal('3FFD0003'))
                    # Now we just need to translate that value.
                    self.__stringEncoding = lookupCodePage(enc)
                return self.__stringEncoding

    @property
    def treePath(self) -> List[weakref.ReferenceType]:
        """
        A path, as a list of weak reference to the instances needed to get to
        this instance through the MSGFile-Attachment tree. These are weak
        references to ensure the garbage collector doesn't see the references
        back to higher objects.
        """
        return self.__treePath
