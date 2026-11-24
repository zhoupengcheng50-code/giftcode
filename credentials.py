"""Store GMS credentials in the current user's Windows Credential Manager."""

import ctypes as ct
from ctypes import wintypes as wt

TARGET = "VietnamGiftWorkbench/GMS"


class Credential(ct.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ct.POINTER(ct.c_ubyte)),
        ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
        ("Attributes", ct.c_void_p), ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


api = ct.WinDLL("Advapi32", use_last_error=True)
api.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.POINTER(ct.POINTER(Credential))]
api.CredWriteW.argtypes = [ct.POINTER(Credential), wt.DWORD]
api.CredFree.argtypes = [ct.c_void_p]


def load():
    result = ct.POINTER(Credential)()
    if not api.CredReadW(TARGET, 1, 0, ct.byref(result)):
        error = ct.get_last_error()
        if error in (1168, 1312):
            return "", ""
        raise ct.WinError(error)
    try:
        value = result.contents
        password = ct.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-16-le")
        return value.UserName, password
    finally:
        api.CredFree(result)


def save(username, password):
    encoded = password.encode("utf-16-le")
    blob = (ct.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = Credential(Type=1, TargetName=TARGET, CredentialBlobSize=len(encoded),
                            CredentialBlob=blob, Persist=2, UserName=username)
    if not api.CredWriteW(ct.byref(credential), 0):
        raise ct.WinError(ct.get_last_error())
