# Simple fallback imghdr module for Python 3.13+
# python-telegram-bot sirf imghdr.what() use karta hai.
# Yahan hum ek basic version de rahe hain.

import mimetypes

def what(file, h=None):
    """
    Try to guess image type from file name using mimetypes.
    Agar kuch samajh na aaye to None return karega.
    """
    if isinstance(file, bytes):
        # Hume bytes analyze nahi karne, None de dete hain.
        return None

    if isinstance(file, str):
        mime, _ = mimetypes.guess_type(file)
        if mime and mime.startswith("image/"):
            # "image/png" -> "png"
            return mime.split("/")[-1]

    return None
