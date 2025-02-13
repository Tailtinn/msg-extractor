from __future__ import annotations


__all__ = [
    'Recipient',
]


import functools
import logging

from typing import (
        Any, Callable, List, Optional, Tuple, TYPE_CHECKING, TypeVar, Union
    )

from .enums import ErrorBehavior, MeetingRecipientType, PropertiesType, RecipientType
from .exceptions import StandardViolationError
from .properties.prop import FixedLengthProp
from .properties.properties_store import PropertiesStore
from .structures.entry_id import PermanentEntryID
from .utils import makeWeakRef, msgPathToString, verifyPropertyId, verifyType


if TYPE_CHECKING:
    from .msg_classes.msg import MSGFile

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_T = TypeVar('_T')


class Recipient:
    """
    Contains the data of one of the recipients in an MSG file.
    """

    def __init__(self, _dir, msg : MSGFile):
        self.__msg = makeWeakRef(msg) # Allows calls to original msg file.
        self.__dir = _dir
        if not self.exists('__properties_version1.0'):
            if ErrorBehavior.STANDARDS_VIOLATION in msg.errorBehavior:
                logger.error('Recipients MUST have a property stream.')
            else:
                raise StandardViolationError('Recipients MUST have a property stream.') from None
        self.__props = PropertiesStore(self.getStream('__properties_version1.0'), PropertiesType.RECIPIENT)
        self.__email = self.getStringStream('__substg1.0_39FE')
        if not self.__email:
            self.__email = self.getStringStream('__substg1.0_3003')
        self.__name = self.getStringStream('__substg1.0_3001')
        self.__typeFlags = self.__props.getValue('0C150003', 0)
        from .msg_classes.calendar_base import CalendarBase
        if isinstance(msg, CalendarBase):
            self.__type = MeetingRecipientType(0xF & self.__typeFlags)
        else:
            self.__type = RecipientType(0xF & self.__typeFlags)
        self.__formatted = f'{self.__name} <{self.__email}>'

    def _getStream(self, filename) -> Optional[bytes]:
        """
        Gets a binary representation of the requested filename.

        This should ALWAYS return a bytes object if it was found, otherwise
        returns None.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        import warnings
        warnings.warn(':method _getStream: has been deprecated and moved to the public api. Use :method getStream: instead (remove the underscore).', DeprecationWarning)
        return self.getStream(filename)

    def _getStringStream(self, filename) -> Optional[str]:
        """
        Gets a string representation of the requested filename.

        Rather than the full filename, you should only feed this function the
        filename sans the type. So if the full name is "__substg1.0_001A001F",
        the filename this function should receive should be "__substg1.0_001A".

        This should ALWAYS return a string if it was found, otherwise returns
        None.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        import warnings
        warnings.warn(':method _getStringStream: has been deprecated and moved to the public api. Use :method getStringStream: instead (remove the underscore).', DeprecationWarning)
        return self.getStringStream(filename)

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

    def _getTypedData(self, _id, _type = None):
        """
        Gets the data for the specified id as the type that it is supposed to
        be. :param id: MUST be a 4 digit hexadecimal string.

        If you know for sure what type the data is before hand, you can specify
        it as being one of the strings in the constant FIXED_LENGTH_PROPS_STRING
        or VARIABLE_LENGTH_PROPS_STRING.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        verifyPropertyId(id)
        _id = _id.upper()
        found, result = self._getTypedStream('__substg1.0_' + _id, _type)
        if found:
            return result
        else:
            found, result = self._getTypedProperty(_id, _type)
            return result if found else None

    def _getTypedProperty(self, propertyID : str, _type = None) -> Tuple[bool, Optional[object]]:
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
            prop = self.props.get(propertyID + _type)
            if isinstance(prop, FixedLengthProp):
                return True, prop.value
            else:
                return False, None
        else:
            props = self.props.getProperties(propertyID)
            for prop in props:
                if isinstance(prop, FixedLengthProp):
                    return True, prop.value

        return False, None

    def _getTypedStream(self, filename, _type = None):
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

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg._getTypedStream(self, [self.__dir, msgPathToString(filename)], True, _type)

    def exists(self, filename) -> bool:
        """
        Checks if stream exists inside the recipient folder.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.exists([self.__dir, msgPathToString(filename)])

    def sExists(self, filename) -> bool:
        """
        Checks if the string stream exists inside the recipient folder.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.sExists([self.__dir, msgPathToString(filename)])

    def existsTypedProperty(self, id, _type = None) -> bool:
        """
        Determines if the stream with the provided id exists. The return of this
        function is 2 values, the first being a boolean for if anything was
        found, and the second being how many were found.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.existsTypedProperty(id, self.__dir, _type, True, self.__props)

    def getMultipleBinary(self, filename) -> Optional[List[bytes]]:
        """
        Gets a multiple binary property as a list of bytes objects.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_00011102" then the
        filename would simply be "__substg1.0_0001".

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.getMultipleBinary([self.__dir, msgPathToString(filename)])

    def getMultipleString(self, filename) -> Optional[List[str]]:
        """
        Gets a multiple string property as a list of str objects.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_00011102" then the
        filename would simply be "__substg1.0_0001".

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.getMultipleString([self.__dir, msgPathToString(filename)])

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

    def getSingleOrMultipleBinary(self, filename) -> Optional[Union[List[bytes], bytes]]:
        """
        A combination of :method getStringStream: and
        :method getMultipleString:.

        Checks to see if a single binary stream exists to return, otherwise
        tries to return the multiple binary stream of the same ID.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_00010102" then the
        filename would simply be "__substg1.0_0001".

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.getSingleOrMultipleBinary([self.__dir, msgPathToString(filename)])

    def getSingleOrMultipleString(self, filename) -> Optional[Union[List[str], str]]:
        """
        A combination of :method getStringStream: and
        :method getMultipleString:.

        Checks to see if a single string stream exists to return, otherwise
        tries to return the multiple string stream of the same ID.

        Like :method getStringStream:, the 4 character type suffix should be
        omitted. So if you want the stream "__substg1.0_0001001F" then the
        filename would simply be "__substg1.0_0001".

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.getSingleOrMultipleString([self.__dir, msgPathToString(filename)])

    def getStream(self, filename) -> Optional[bytes]:
        """
        Gets a binary representation of the requested filename.

        This should ALWAYS return a bytes object if it was found, otherwise
        returns None.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.getStream([self.__dir, msgPathToString(filename)])

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

    def getStringStream(self, filename) -> Optional[str]:
        """
        Gets a string representation of the requested filename.

        Rather than the full filename, you should only feed this function the
        filename sans the type. So if the full name is "__substg1.0_001A001F",
        the filename this function should receive should be "__substg1.0_001A".

        This should ALWAYS return a string if it was found, otherwise returns
        None.

        :raises ReferenceError: The associated MSGFile instance has been garbage
            collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The msg file for this Recipient instance has been garbage collected.')
        return msg.getStringStream([self.__dir, msgPathToString(filename)])

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

    @functools.cached_property
    def account(self) -> Optional[str]:
        """
        Returns the account of this recipient.
        """
        return self.getStringStream('__substg1.0_3A00')

    @property
    def email(self) -> Optional[str]:
        """
        Returns the recipient's email.
        """
        return self.__email

    @functools.cached_property
    def entryID(self) -> Optional[PermanentEntryID]:
        """
        Returns the recipient's Entry ID.
        """
        return self.getStreamAs('__substg1.0_0FFF0102', PermanentEntryID)

    @property
    def formatted(self) -> str:
        """
        Returns the formatted recipient string.
        """
        return self.__formatted

    @functools.cached_property
    def instanceKey(self) -> Optional[bytes]:
        """
        Returns the instance key of this recipient.
        """
        return self.getStream('__substg1.0_0FF60102')

    @property
    def name(self) -> Optional[str]:
        """
        Returns the recipient's name.
        """
        return self.__name

    @property
    def props(self) -> PropertiesStore:
        """
        Returns the Properties instance of the recipient.
        """
        return self.__props

    @functools.cached_property
    def recordKey(self) -> Optional[bytes]:
        """
        Returns the instance key of this recipient.
        """
        return self.getStream('__substg1.0_0FF90102')

    @functools.cached_property
    def searchKey(self) -> Optional[bytes]:
        """
        Returns the search key of this recipient.
        """
        return self.getStream('__substg1.0_300B0102')

    @functools.cached_property
    def smtpAddress(self) -> Optional[str]:
        """
        Returns the SMTP address of this recipient.
        """
        return self.getStringStream('__substg1.0_39FE')

    @functools.cached_property
    def transmittableDisplayName(self) -> Optional[str]:
        """
        Returns the transmittable display name of this recipient.
        """
        return self.getStringStream('__substg1.0_3A20')

    @property
    def type(self) -> Union[RecipientType, MeetingRecipientType]:
        """
        Returns the recipient type. Type is:
            * Sender if `type & 0xf == 0`
            * To if `type & 0xf == 1`
            * Cc if `type & 0xf == 2`
            * Bcc if `type & 0xf == 3`
        """
        return self.__type

    @property
    def typeFlags(self) -> int:
        """
        The raw recipient type value and all the flags it includes.
        """
        return self.__typeFlags
