"""Read a Windows clipboard image and encode it as PNG.

Uses only the standard library so it can run inside Sublime Text's plugin
host. The vendored Pillow build linked against python33.dll cannot load on
newer plugin hosts (Python 3.8+ / 3.14).
"""

import struct
import time
import zlib
from ctypes import WinDLL, c_size_t, c_void_p, string_at, wintypes

CF_DIB = 8
CF_DIBV5 = 17
BI_RGB = 0
BI_BITFIELDS = 3

_user32 = WinDLL("user32", use_last_error=True)
_kernel32 = WinDLL("kernel32", use_last_error=True)

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT

_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalSize.restype = c_size_t


def read_clipboard_png():
	"""Return PNG bytes from the clipboard, or None when it has no image."""
	png_formats = []
	for name in ("PNG", "image/png"):
		fmt = _user32.RegisterClipboardFormatW(name)
		if fmt:
			png_formats.append(fmt)

	opened = False
	for _ in range(10):
		if _user32.OpenClipboard(None):
			opened = True
			break
		time.sleep(0.02)
	if not opened:
		return None

	try:
		for fmt in png_formats:
			raw = _read_format(fmt)
			if raw and raw.startswith(b"\x89PNG\r\n\x1a\n"):
				return raw
		for fmt in (CF_DIBV5, CF_DIB):
			raw = _read_format(fmt)
			if not raw:
				continue
			encoded = _dib_to_png(raw)
			if encoded:
				return encoded
		return None
	finally:
		_user32.CloseClipboard()


def _read_format(fmt):
	if not _user32.IsClipboardFormatAvailable(fmt):
		return None
	handle = _user32.GetClipboardData(fmt)
	if not handle:
		return None
	size = _kernel32.GlobalSize(handle)
	if not size:
		return None
	ptr = _kernel32.GlobalLock(handle)
	if not ptr:
		return None
	try:
		return string_at(ptr, size)
	finally:
		_kernel32.GlobalUnlock(handle)


def _dib_to_png(data):
	parsed = _parse_dib(data)
	if parsed is None:
		return None
	width, height, rows = parsed
	return _encode_png(width, height, rows)


def _parse_dib(data):
	if len(data) < 40:
		return None
	header_size = struct.unpack_from("<I", data, 0)[0]
	if header_size < 40 or header_size > len(data):
		return None

	width, height, planes, bit_count, compression = struct.unpack_from("<iiHHI", data, 4)
	top_down = height < 0
	height = abs(height)
	if planes != 1 or width <= 0 or height <= 0:
		return None
	if bit_count not in (24, 32) or compression not in (BI_RGB, BI_BITFIELDS):
		return None

	offset = header_size
	red_mask = green_mask = blue_mask = alpha_mask = 0
	if compression == BI_BITFIELDS:
		if header_size >= 56:
			red_mask, green_mask, blue_mask, alpha_mask = struct.unpack_from("<IIII", data, 40)
		elif header_size == 40 and len(data) >= 52:
			red_mask, green_mask, blue_mask = struct.unpack_from("<III", data, 40)
			offset = 52
		else:
			return None
	elif bit_count == 24:
		red_mask, green_mask, blue_mask = 0x0000FF, 0x00FF00, 0xFF0000
	else:
		red_mask, green_mask, blue_mask, alpha_mask = 0x0000FF, 0x00FF00, 0xFF0000, 0xFF000000

	# BI_RGB channel order in memory is B,G,R, so the numeric masks above are
	# little-endian byte masks. BITFIELDS masks are already in that form.

	stride = ((width * bit_count + 31) // 32) * 4
	pixels = data[offset:]
	if len(pixels) < stride * height:
		return None

	bpp = bit_count // 8
	use_straight_bgr = compression == BI_RGB
	rows = []
	alpha_nonzero = False
	for y in range(height):
		src_y = y if top_down else height - 1 - y
		src = pixels[src_y * stride:(src_y + 1) * stride]
		out = bytearray(width * 4)
		for x in range(width):
			pix = src[x * bpp:(x + 1) * bpp]
			if use_straight_bgr:
				b, g, r = pix[0], pix[1], pix[2]
				a = pix[3] if bit_count == 32 else 255
			else:
				value = int.from_bytes(pix, "little")
				r = _mask_channel(value, red_mask)
				g = _mask_channel(value, green_mask)
				b = _mask_channel(value, blue_mask)
				a = _mask_channel(value, alpha_mask) if alpha_mask else 255
			if a:
				alpha_nonzero = True
			base = x * 4
			out[base:base + 4] = bytes((r, g, b, a))
		rows.append(out)

	# Win32 screenshots often store 32-bit DIBs with every alpha byte left at 0.
	if bit_count == 32 and not alpha_nonzero:
		for row in rows:
			for x in range(width):
				row[x * 4 + 3] = 255
	return width, height, rows


def _mask_channel(value, mask):
	if not mask:
		return 0
	shift = (mask & -mask).bit_length() - 1
	bits = bin(mask).count("1")
	channel = (value & mask) >> shift
	if bits == 8:
		return channel
	if bits > 8:
		return channel >> (bits - 8)
	return (channel * 255) // ((1 << bits) - 1)


def _encode_png(width, height, rows):
	raw = b"".join(b"\x00" + bytes(row) for row in rows)

	def chunk(tag, payload):
		crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
		return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

	ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
	return b"".join((
		b"\x89PNG\r\n\x1a\n",
		chunk(b"IHDR", ihdr),
		chunk(b"IDAT", zlib.compress(raw, 6)),
		chunk(b"IEND", b""),
	))
