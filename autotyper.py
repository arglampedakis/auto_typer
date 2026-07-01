#!/usr/bin/env python3
r"""
autotyper.py - Windows clipboard auto-typer that survives "smart" editors.

What it does
------------
* Global hotkey  Ctrl+Shift+Z  (works even when this console is not focused).
* On trigger: counts down 3... 2... 1... so you can click into the target
  window, then types the current clipboard text into whatever window has
  focus, character by character (so it looks typed, not pasted).
* The typed result is byte-for-byte identical to the clipboard - same
  indentation, same line breaks - EVEN in editors with auto-indent,
  auto-closing brackets/quotes, or reindent-on-type (VS Code, IntelliJ,
  JSON editors, ...).
* Returns to listening afterwards, so you can trigger it again.
* Ctrl+C in this console quits cleanly.

Dependencies: NONE - pure Python standard library (ctypes + Win32). The global
hotkey uses RegisterHotKey (no keyboard hook), the clipboard is read via the
Win32 clipboard API, and typing uses SendInput. Avoiding a keyboard hook and
third-party packages keeps a compiled build from looking like a keylogger to
antivirus.

Why it does not corrupt indentation (the bug this tool exists to avoid)
-----------------------------------------------------------------------
A naive typer sends the Enter/Tab VIRTUAL KEYS. A smart editor reacts to the
Enter *key* by auto-indenting the new line to match the previous one; you then
add your own leading spaces on top, so indentation grows on every line and the
output drifts further right line after line. This tool instead:

  1. Injects every printable character (and every leading space) as a raw
     Unicode code unit via the Win32 SendInput API with KEYEVENTF_UNICODE.
     That is a pure "insert this character" event with no key semantics, so the
     editor cannot interpret it as a command.
  2. Line breaks are editor-dependent (auto-detected; see detect_profile).
     Monaco/Electron ignore a Unicode newline, so they need the real Enter key
     (VK_RETURN); Notepad++ instead needs a Unicode newline, because its
     autocomplete popup eats the Enter key. On editors that auto-indent, the
     indentation the newline adds is selected (End, Shift+Home) and deleted -
     leaving a truly empty line - before the line's own exact whitespace is
     typed, so indentation never compounds. Plain editors (Windows Notepad)
     don't auto-indent and are typed without any of steps 2-4.
  3. Auto-closed partners are neutralised: right after an opener ( [ { " ' `
     a forward-Delete removes the bracket/quote the editor auto-inserted
     (a harmless no-op when it inserted nothing).
  4. Lines that begin with a closing bracket would be re-indented by the editor
     the moment that bracket is typed. A throwaway sentinel digit is typed at
     column 0 first so the bracket is never the line's first non-whitespace
     character (no reindent fires in any editor); the sentinel is then deleted.

Tested on Windows 11 with byte-for-byte identical results against VS Code
(Monaco), Notepad++, and the new Windows Notepad - see the project notes.
"""

import ctypes
import sys
import time
from ctypes import wintypes

# This tool uses ONLY the Python standard library (ctypes + Win32). It has no
# third-party dependencies on purpose: the global hotkey uses RegisterHotKey
# (no keyboard hook) and the clipboard is read through the Win32 clipboard API.
# That keeps a compiled build from looking like a keylogger to antivirus.


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
HOTKEY = "ctrl+shift+z"
COUNTDOWN_SECONDS = 3

# Delays. Unicode character insertion is reliable even when fast, but Electron
# editors (VS Code) drop virtual-key events (Enter/Home/Delete/...) when they
# arrive too quickly, so the structural keys need a more generous gap.
CHAR_DELAY = 0.004   # pause after each typed character
KEY_DELAY = 0.035    # pause after each structural key (Enter / Home / Delete)

# How to insert line breaks: None = auto-detect from the focused window
# (recommended), or force "vk" (the Enter key - works in VS Code, the new
# Windows Notepad, most editors) or "char" (a Unicode newline - needed for
# Notepad++, whose autocomplete would otherwise eat the Enter key).
NEWLINE_MODE = None

# Whether to neutralise auto-indent / auto-close / reindent. None = auto-detect
# (on for IDEs & Notepad++, off for plain editors like Windows Notepad). Force
# True for an IDE that isn't detected, or False for a plain editor.
NEUTRALIZE = None


# --------------------------------------------------------------------------- #
# Win32 SendInput plumbing
# --------------------------------------------------------------------------- #
user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTunion(ctypes.Union):
    # Must include the largest member (MOUSEINPUT) so sizeof(INPUT) == 40 on
    # x64; otherwise SendInput fails with ERROR_INVALID_PARAMETER (87).
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Virtual key codes (used only for navigation, never to type characters).
VK_BACK = 0x08
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_END = 0x23
VK_HOME = 0x24
VK_DELETE = 0x2E

# Keys in the keyboard's "extended" block. They MUST carry KEYEVENTF_EXTENDEDKEY
# or Windows can treat them as the numpad equivalents and Shift fails to compose
# with them (e.g. Shift+Home stops selecting).
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}

# Characters that make a smart editor auto-insert a closing partner.
OPENERS = set("([{\"'`")
# Characters that make a smart editor re-indent the line when typed first.
LEADING_REINDENT = set(")]}")

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)


def _scan_code(vk):
    return user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC


def _send(*inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    if user32.SendInput(n, arr, ctypes.sizeof(INPUT)) != n:
        raise ctypes.WinError(ctypes.get_last_error())


def _unicode_input(code_unit, key_up=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    ki = KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=None)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


def _vk_input(vk, key_up=False):
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk in EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    # Carry the real hardware scan code. Injected key events without a scan code
    # are unreliable in classic Win32 / Scintilla controls (Notepad, Notepad++):
    # key-downs get dropped, the new line is never created, and the line-clear
    # then runs on the line just typed - wiping it. Real keys always carry both
    # a virtual key and a scan code, so we mirror that.
    ki = KEYBDINPUT(wVk=vk, wScan=_scan_code(vk), dwFlags=flags, time=0, dwExtraInfo=None)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


def _utf16_code_units(s):
    b = s.encode("utf-16-le")
    for i in range(0, len(b), 2):
        yield b[i] | (b[i + 1] << 8)


def send_char(ch):
    """Insert one character (handles non-BMP code points via surrogate pairs)."""
    for cu in _utf16_code_units(ch):
        _send(_unicode_input(cu, False), _unicode_input(cu, True))


def tap(vk, gap=0.006):
    # Send key-down and key-up as SEPARATE events with a small gap. Batching
    # both in one SendInput makes some apps miss the keypress (the up can be
    # processed before the down registers) - which dropped Enters and corrupted
    # output in Notepad/Notepad++.
    _send(_vk_input(vk, False)); time.sleep(gap)
    _send(_vk_input(vk, True)); time.sleep(gap)


def tap_with_shift(vk, hold=0.008):
    # Send Shift-down, the key, then Shift-up as SEPARATE events with small
    # gaps. Batching modifier+key in one SendInput is unreliable in Electron
    # apps: the key may be processed before the modifier's down-state registers,
    # so Shift+Home degrades into a plain Home (no selection).
    _send(_vk_input(VK_SHIFT, False)); time.sleep(hold)
    _send(_vk_input(vk, False)); time.sleep(hold)
    _send(_vk_input(vk, True)); time.sleep(hold)
    _send(_vk_input(VK_SHIFT, True)); time.sleep(hold)


def release_modifiers():
    """Force Ctrl/Shift/Alt to the up state so a still-held hotkey cannot turn
    our navigation keys into Ctrl+Home / Ctrl+Delete / etc."""
    for vk in (VK_CONTROL, VK_SHIFT, VK_MENU):
        _send(_vk_input(vk, True))


# --------------------------------------------------------------------------- #
# The smart-editor-safe typer
# --------------------------------------------------------------------------- #
def _newline(mode):
    """Insert a line break. Two editor families need different methods:
      'char' : a Unicode newline character (KEYEVENTF_UNICODE). Native Windows
               editors (Notepad, Notepad++/Scintilla, most controls) insert it
               as text - crucially it is NOT intercepted by their autocomplete
               popups (which would otherwise accept a suggestion instead of
               making a newline, then our line-clear would wipe the typed line).
      'vk'   : the real Enter key (VK_RETURN). Required by Monaco/Electron
               editors (VS Code), which ignore a Unicode newline entirely.
    """
    if mode == "char":
        send_char("\n")
    else:
        tap(VK_RETURN)


def detect_profile():
    """Choose how to type into the focused window. Returns (newline_mode, neutralize).

    Three families behave differently:
      * Notepad++ - auto-indents AND its autocomplete eats the Enter key, so use
        a Unicode newline ('char', which the popup ignores) and neutralise
        auto-indent/auto-close.
      * Plain editors (Windows Notepad, classic edit controls) - no auto-indent,
        no auto-close, no autocomplete: the Enter key works and NO neutralisation
        is needed. Neutralising here is actively harmful (the new Windows Notepad
        merges lines when our line-clear issues a Delete), so it is turned off.
      * Everything else (VS Code / Monaco, IDEs) - the Enter key works and we
        neutralise auto-indent/auto-close/reindent.

    Override via NEWLINE_MODE / NEUTRALIZE below if a particular editor needs it.
    """
    try:
        hwnd = user32.GetForegroundWindow()
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls_v = cls.value
        blob = (cls_v + " " + title.value).lower()
        if "notepad++" in blob:
            return "char", True
        if cls_v == "Notepad" or "windowsnotepad" in blob:   # plain Notepad
            return "vk", False
        return "vk", True
    except Exception:
        return "vk", True


def type_text(text, char_delay=CHAR_DELAY, key_delay=KEY_DELAY, neutralize=None,
              newline_mode=None):
    """Type `text` into the focused window, byte-for-byte, defeating auto-indent,
    auto-close and reindent-on-type (see module docstring).

    newline_mode / neutralize: None to auto-detect from the focused window.
    """
    auto_nl, auto_neut = detect_profile()
    if newline_mode is None:
        newline_mode = NEWLINE_MODE or auto_nl
    if neutralize is None:
        neutralize = auto_neut if NEUTRALIZE is None else NEUTRALIZE
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    for i, line in enumerate(lines):
        if i > 0:
            _newline(newline_mode)
            time.sleep(key_delay)
            if neutralize:
                # Defeat auto-indent: select the whole (auto-indented but
                # otherwise empty) new line and delete it, so typing starts at
                # column 0 and indentation never compounds. Only enabled for
                # editors that actually auto-indent (see detect_profile); plain
                # editors run with neutralize=False and skip this entirely.
                tap(VK_END); time.sleep(key_delay)
                tap_with_shift(VK_HOME); time.sleep(key_delay)
                tap(VK_DELETE); time.sleep(key_delay)

        # Lines beginning with a closing bracket get re-indented by smart
        # editors the instant the bracket is typed. Prevent it from ever firing
        # (portable across editors) by typing a throwaway sentinel digit at
        # column 0 first, so the bracket is not the line's first non-whitespace
        # character. The sentinel is removed afterwards.
        sentinel = neutralize and line.lstrip(" \t")[:1] in LEADING_REINDENT
        if sentinel:
            send_char("0"); time.sleep(char_delay)

        for ch in line:
            send_char(ch); time.sleep(char_delay)
            if neutralize and ch in OPENERS:
                tap(VK_DELETE); time.sleep(key_delay)  # kill auto-closed partner

        if sentinel:
            tap(VK_HOME); time.sleep(key_delay)
            tap(VK_HOME); time.sleep(key_delay)   # double tap -> guaranteed col 0
            tap(VK_DELETE); time.sleep(key_delay)  # remove the sentinel
            tap(VK_END); time.sleep(key_delay)


# --------------------------------------------------------------------------- #
# Clipboard (Win32) - read plain Unicode text without pyperclip
# --------------------------------------------------------------------------- #
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
CF_UNICODETEXT = 13
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.CloseClipboard.restype = wintypes.BOOL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)


def get_clipboard_text():
    """Return the clipboard's plain Unicode text ("" if none)."""
    for _ in range(10):                       # clipboard can be briefly locked
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- #
# Global hotkey via RegisterHotKey (no keyboard hook -> not keylogger-like)
# --------------------------------------------------------------------------- #
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT,
         "alt": MOD_ALT, "win": MOD_WIN, "cmd": MOD_WIN}

user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.PeekMessageW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT)
user32.PeekMessageW.restype = wintypes.BOOL


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


def parse_hotkey(spec):
    """'ctrl+shift+z' -> (modifiers, virtual_key_code)."""
    mods, vk = 0, None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x70 + int(part[1:]) - 1          # F1..F24
        else:
            raise ValueError(f"unrecognised hotkey token: {part!r}")
    if vk is None:
        raise ValueError(f"no main key in hotkey: {spec!r}")
    return mods | MOD_NOREPEAT, vk


def do_type():
    text = get_clipboard_text()
    if not text:
        print("\n[!] Clipboard is empty - nothing to type.")
        return
    print(f"\n[+] Triggered. Typing {len(text)} chars / "
          f"{text.count(chr(10)) + 1} lines after countdown - switch to your target window:")
    for n in range(COUNTDOWN_SECONDS, 0, -1):
        print(f"    {n}...", end="", flush=True)
        time.sleep(1)
    print(" go!")
    release_modifiers()          # in case the hotkey keys are still held
    time.sleep(0.05)
    type_text(text)
    print(f"[+] Done. Waiting for the next {HOTKEY.upper()} (Ctrl+C to quit).")


def main():
    print("=" * 64)
    print("  autotyper - clipboard -> keystrokes (smart-editor safe)")
    print("=" * 64)
    print(f"  Hotkey      : {HOTKEY.upper()}  (global)")
    print(f"  Countdown   : {COUNTDOWN_SECONDS}s before typing")
    print("  Quit        : Ctrl+C (or close this window)")
    print("-" * 64)

    try:
        mods, vk = parse_hotkey(HOTKEY)
    except ValueError as exc:
        sys.exit(f"Bad HOTKEY setting: {exc}")

    hotkey_id = 1
    if not user32.RegisterHotKey(None, hotkey_id, mods, vk):
        sys.exit(f"Could not register {HOTKEY.upper()} - another program may "
                 f"already use it. Change HOTKEY at the top of the script.")

    print("Copy text, press the hotkey, then click into the target window.")
    print(f"Listening for {HOTKEY.upper()} ...")
    msg = MSG()
    try:
        while True:
            # PeekMessage keeps the loop interruptible by Ctrl+C (a blocking
            # GetMessage would not be). WM_HOTKEY is posted to this thread.
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    try:
                        do_type()
                    except Exception as exc:      # never let the loop die
                        print(f"\n[!] Error while typing: {exc!r}")
                    # discard any hotkey presses queued while we were typing
                    while user32.PeekMessageW(ctypes.byref(MSG()), None,
                                              WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
                        pass
            else:
                time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nExiting. Bye.")
    finally:
        user32.UnregisterHotKey(None, hotkey_id)


if __name__ == "__main__":
    main()
