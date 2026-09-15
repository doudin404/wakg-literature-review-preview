"""Serialize local PyMuPDF access; model calls remain concurrent."""
from functools import wraps
from threading import RLock

pdf_lock=RLock()

def serialized_pdf(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        with pdf_lock:return function(*args,**kwargs)
    return wrapped
