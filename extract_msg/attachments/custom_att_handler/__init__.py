from __future__ import annotations


"""
Submodule designed to help with saving and using custom attachments. Custom
attachments are those follow standards not defined in the MSG documentation. Use
the function `getHandler` to get an instance of a subclass of
CustomAttachmentHandler.

CustomAttachmentHandler subclasses will all define the following methods:
    injectHtml: A method which takes HTML and inserts the

It should hopefully be completely unnecessary for your code to know what type of
handler it is using, as the abstract base class should give all of the functions
you would typically want.

If you would like to add your own handler, simply subclass
CustomAttachmentHandler and add it using the `registerHandler` function.
"""

__all__ = [
    # Classes.
    'CustomAttachmentHandler',
    'OutlookImageDIB',

    # Functions.
    'getHandler',
    'registerHandler',
]


from typing import List, Type, TYPE_CHECKING

from .custom_handler import CustomAttachmentHandler


# Create a way to register handlers.
_knownHandlers : List[CustomAttachmentHandler] = []

def registerHandler(handler : Type[CustomAttachmentHandler]) -> None:
    """
    Registers the CustomAttachmentHandler subclass as a handler.

    :raises TypeError: The handler was not a subclass of
        CustomAttachmentHandler.
    """
    # Make sure it is a subclass of CustomAttachmentHandler.
    if not isinstance(handler, type):
        raise ValueError(':param handler: must be a class, not an instance of a class.')
    if not issubclass(handler, CustomAttachmentHandler):
        raise ValueError(':param handler: must be a subclass of CustomAttachmentHandler.')
    _knownHandlers.append(handler)



# Import built-in handler modules. They will all automatically register their
# respecive handler(s).
from .outlook_image_dib import OutlookImageDIB


if TYPE_CHECKING:
    from ..attachment_base import AttachmentBase


# Function designed to route to the correct handler.
def getHandler(attachment : AttachmentBase) -> CustomAttachmentHandler:
    """
    Takes an attachment and uses it to find the correct handler. Returns an
    instance created using the specified attachment.

    :raises NotImplementedError: No handler could be found.
    :raises ValueError: A handler was found, but something was wrong with the
        attachment data.
    """
    for handler in _knownHandlers:
        if handler.isCorrectHandler(attachment):
            return handler(attachment)

    raise NotImplementedError('No valid handler could be found for the attachment. Contact the developers for help.')
