import sublime
import sublime_plugin
from collections import defaultdict
import struct
import base64
import urllib.request
import urllib.parse
import io
import os.path
import re
import shutil
import subprocess
import sys
import threading


DEBUG = False

def debug(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

settings_file = 'MarkdownImages.sublime-settings'


def get_settings():
    return sublime.load_settings(settings_file)


class MarkdownImagesPlugin(sublime_plugin.EventListener):

    def on_load(self, view):
        settings = get_settings()
        show_local = settings.get('show_local_images_on_load', False)
        show_remote = settings.get('show_remote_images_on_load', False)
        if not show_local and not show_remote:
            return
        if not self._should_run_for_extension(settings, view):
            return
        self._update_images(settings,
                            view,
                            show_local=show_local,
                            show_remote=show_remote)

    def on_post_save(self, view):
        settings = get_settings()
        show_local = settings.get('show_local_images_on_post_save')
        show_remote = settings.get('show_remote_images_on_post_save')
        if not show_local and not show_remote:
            return
        if not self._should_run_for_extension(settings, view):
            return
        self._update_images(settings,
                            view,
                            show_local=show_local,
                            show_remote=show_remote)

    def on_close(self, view):
        ImageHandler.on_close(view)

    def _should_run_for_extension(self, settings, view):
        extensions = settings.get('extensions')
        fn = view.file_name()
        _, ext = os.path.splitext(fn)
        # extensions can be either a list or single string
        if isinstance(extensions, str):
            return ext == extensions
        return ext in extensions

    def _update_images(self, settings, view, **kwargs):
        max_width = settings.get('img_maxwidth', None)
        base_path = settings.get('base_path', None)
        ImageHandler.hide_images(view)
        ImageHandler.show_images(view,
                                 max_width=max_width,
                                 show_local=kwargs.get('show_local', False),
                                 show_remote=kwargs.get('show_remote', False),
                                 base_path=base_path)


class ImageHandler:
    """
    Static class to bundle image handling.
    """

    selector = 'markup.underline.link.image.markdown'
    # Maps view IDs to sets of phantom (key, html) pairs
    phantoms = defaultdict(set)
    # Cached remote URL image data. Kept even if not rendered.
    urldata = defaultdict(dict)
    # Bumped each time show_images runs, so a slow download cannot update a closed view.
    generation = {}

    @staticmethod
    def on_close(view):
        ImageHandler.generation[view.id()] = ImageHandler.generation.get(view.id(), 0) + 1
        ImageHandler._erase_phantoms(view)
        ImageHandler.urldata.pop(view.id(), None)

    @staticmethod
    def show_images(view, max_width=None, show_local=True, show_remote=False, base_path=""):
        debug("show_images")
        if not show_local and not show_remote:
            debug("doing nothing")
            return
        # Note: Excessive size will cause the ST3 window to become blank
        # unrecoverably. 1024 apperas to be a safe limit,
        # but can possibly go higher.
        if not max_width or max_width < 0:
            max_width = 1024

        skip = 0

        phantoms = {}
        pending_remote = []
        generation = ImageHandler.generation.get(view.id(), 0) + 1
        ImageHandler.generation[view.id()] = generation
        # Sublime's Markdown syntax ends an image URL at the first space, so
        # "![](./imgs/jinjia2 生成html0.png)" only scopes "./imgs/jinjia2".
        # Read the whole destination inside the parentheses instead.
        img_regs = image_refs(view)

        for region, rel_p, scale in reversed(img_regs):
            ttype = None
            urldata = None

            # (Windows) cutting the drive letter from the path,
            # otherwise urlparse interprets it as a scheme (like 'file' or 'http')
            # and generates a bogus url object like:
            # url= ParseResult(scheme='c', netloc='', path='/path/image.png', params='', query='', fragment='')
            drive_letter, rel_p = os.path.splitdrive(rel_p)

            url = urllib.parse.urlparse(rel_p)
            if url.scheme and url.scheme != 'file':
                if not show_remote or url.scheme not in ('http', 'https'):
                    continue

                # We can't render SVG images, so skip the request
                # Note: not all URLs that return SVG end with .svg
                # We could use a HEAD request to check the Content-Type before
                # downloading the image, but the size of an SVG is typically
                # so small to not be worth the extra request
                if url.path.endswith('.svg'):
                    continue

                debug("image url", rel_p)
                line_b = view.line(region).b
                key = 'mdimage-' + str(line_b)
                data = ImageHandler.urldata[view.id()].get(rel_p)
                if not data:
                    for old in ImageHandler.phantoms[view.id()]:
                        if old[0] == key:
                            phantoms[key] = old
                            break
                    pending_remote.append((rel_p, scale, line_b, key))
                    continue

                try:
                    w, h, ttype = get_image_size(io.BytesIO(data))
                except Exception as e:
                    msg = "Failed to get_image_size for data from URL {}"
                    debug(msg.format(rel_p), e)
                    continue

                FMT = u'''
                    <img src="data:image/{}" class="centerImage" {}>
                '''
                b64_data = base64.encodebytes(data).decode('ascii')
                b64_data = b64_data.replace('\n', '')

                img = "{};base64,{}".format(ttype, b64_data)
                urldata = data
            else:
                if not show_local:
                    continue

                # Convert relative paths to be relative to the current file
                # or project folder.
                # NOTE: if the current file or project folder can't be
                # determined (e.g. if the view content is not in a project and
                # hasn't been saved), then it will anchor to /.
                # Typora writes spaces as %20. The file on disk is "AI agent-3.png".
                path = urllib.parse.unquote(url.path)

                # Force paths to be prefixed with base_path if it was provided
                # in settings.
                if base_path:
                    path = os.path.join(base_path, path)

                if not os.path.isabs(path):
                    folder = get_path_for(view)
                    path = os.path.join(folder, path)
                path = os.path.normpath(path)
                # (Windows) Adding back the drive letter that was cut from the path before
                path = drive_letter + path
                if not os.path.isfile(path):
                    alt = find_image_file(get_path_for(view), urllib.parse.unquote(url.path))
                    if alt:
                        path = alt

                # Embed the file instead of using a file:// URL. urlunparse() turns
                # "D:\...\中文.png" into "file:D:\...\中文.png", which minihtml cannot
                # load, so the phantom shows a broken image.
                FMT = u'''
                    <img src="data:image/{}" class="centerImage" {}>
                '''
                try:
                    with open(path, 'rb') as f:
                        data = f.read()
                    data = webp_to_png(data) or data
                    w, h, ttype = get_image_size(io.BytesIO(data))
                except Exception as e:
                    debug("Failed to load {}:".format(path), e)
                    continue
                if ttype:
                    b64_data = base64.encodebytes(data).decode('ascii').replace('\n', '')
                    img = "{};base64,{}".format(ttype, b64_data)

            if not ttype:
                debug("unknown ttype")
                continue

            # TODO -- handle custom sizes better
            # If only width or height are provided, scale the other dimension
            # properly
            # Width defined in custom size should override max_width
            line_region = view.line(region)
            imgattr = check_imgattr(view, line_region, region)
            if not imgattr and w > 0 and h > 0:
                if scale:
                    w *= scale
                    h *= scale
                if max_width and w > max_width:
                    m = max_width / w
                    h *= m
                    w = max_width
                imgattr = 'width="{}" height="{}"'.format(w, h)

            # Force the phantom image view to append past the end of the line
            # Otherwise, the phantom image view interlaces in between
            # word-wrapped lines
            line_region.a = line_region.b

            debug("region", region)
            debug("line_region", line_region)

            key = 'mdimage-' + str(line_region.b)
            html_img = FMT.format(img, imgattr)

            phantom = (key, html_img)
            phantoms[phantom[0]] = phantom
            if phantom in ImageHandler.phantoms[view.id()]:
                debug("Phantom unchanged")
                continue

            debug("Creating phantom", phantom[0])
            view.add_phantom(phantom[0],
                             sublime.Region(line_region.b),
                             phantom[1],
                             sublime.LAYOUT_BLOCK)
            ImageHandler.phantoms[view.id()].add(phantom)
            if urldata is not None:
                ImageHandler.urldata[view.id()][rel_p] = urldata

        # Erase leftover phantoms
        for p in list(ImageHandler.phantoms[view.id()]):
            if phantoms.get(p[0]) != p:
                view.erase_phantoms(p[0])
                ImageHandler.phantoms[view.id()].remove(p)

        if not ImageHandler.phantoms[view.id()]:
            ImageHandler.phantoms.pop(view.id(), None)

        if pending_remote:
            threading.Thread(
                target=load_remote_images,
                args=(view.id(), generation, pending_remote, max_width),
                daemon=True,
            ).start()

    @staticmethod
    def hide_images(view):
        ImageHandler._erase_phantoms(view)

    @staticmethod
    def _erase_phantoms(view):
        for p in ImageHandler.phantoms[view.id()]:
            view.erase_phantoms(p[0])
        ImageHandler.phantoms.pop(view.id(), None)
        # Cached URL data is kept

def image_refs(view):
    """Markdown images and HTML <img> tags, in buffer order."""
    refs = []
    for region in view.find_all(r'!\[[^\]]*\]\((<[^>\n]+>|[^)\n]+)\)'):
        text = view.substr(region)
        match = re.match(r'!\[[^\]]*\]\((.*)\)\s*$', text)
        if not match:
            continue
        dest = image_destination(match.group(1))
        if dest:
            refs.append((region, dest, None))
    for region in view.find_all(r'(?i)<img\b[^>]*>'):
        src, scale = html_image(view.substr(region))
        if src:
            refs.append((region, src, scale))
    return refs


def html_image(tag):
    """Return (src, zoom scale) from an <img> tag. scale is None when unset."""
    match = re.search(
        r'''src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''',
        tag,
        re.I,
    )
    if not match:
        return None, None
    src = next(group for group in match.groups() if group is not None).strip()
    zoom = re.search(r'zoom\s*:\s*(\d+(?:\.\d+)?)\s*%', tag, re.I)
    scale = float(zoom.group(1)) / 100.0 if zoom else None
    return src, scale


def find_image_file(folder, relative):
    """Locate a relative image that is not beside the Markdown file.

    Notes moved out of a subfolder often still say imgs/name.png while the
    file lives in a nested imgs directory.
    """
    if not folder or not relative:
        return None
    direct = os.path.normpath(os.path.join(folder, relative))
    if os.path.isfile(direct):
        return direct
    name = os.path.basename(relative)
    if not name:
        return None
    bases = [folder]
    parent = os.path.dirname(folder)
    if parent and parent != folder:
        bases.append(parent)
    found = []
    for base in bases:
        candidate = os.path.join(base, 'imgs', name)
        if os.path.isfile(candidate):
            return candidate
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for entry in entries:
            candidate = os.path.join(base, entry, 'imgs', name)
            if os.path.isfile(candidate):
                found.append(candidate)
    if len(found) == 1:
        return found[0]
    return None


def image_destination(raw):
    """Path or URL from a Markdown image destination, including spaces."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '<' and raw[-1] == '>':
        return raw[1:-1].strip()
    titled = re.match(r'''^(.*\S)\s+(?:"[^"]*"|'[^']*')\s*$''', raw)
    if titled:
        return titled.group(1).strip()
    return raw


def check_imgattr(view, line_region, link_region=None):
    # find attrs for this link
    full_line = view.substr(line_region)
    link_till_eol = full_line[link_region.a - line_region.a:]
    # find attr if present
    m = re.match(r'.*\)\{(.*)\}', link_till_eol)
    if m:
        return m.groups()[0]
    return ''


def get_file_image_size(img):
    with open(img, 'rb') as f:
        return get_image_size(f)


def _image_type(head):
    """Image type from a header. The imghdr module was removed in Python 3.13."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[6:10] in (b"JFIF", b"Exif"):
        return "jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def _webp_size(head):
    if len(head) < 25 or head[0:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    kind = head[12:16]
    if kind == b"VP8X" and len(head) >= 30:
        width = 1 + int.from_bytes(head[24:27], "little")
        height = 1 + int.from_bytes(head[27:30], "little")
        return width, height
    if kind == b"VP8 " and len(head) >= 30:
        width = int.from_bytes(head[26:28], "little") & 0x3FFF
        height = int.from_bytes(head[28:30], "little") & 0x3FFF
        return width, height
    if kind == b"VP8L" and len(head) >= 25:
        b1, b2, b3, b4 = head[21:25]
        width = 1 + (((b2 & 0x3F) << 8) | b1)
        height = 1 + (((b4 & 0x0F) << 10) | (b3 << 2) | ((b2 & 0xC0) >> 6))
        return width, height
    return None


def _converter_exe(name):
    found = shutil.which(name)
    if found:
        return found
    if name == "magick":
        for path in (
            r"C:\software\scoop\shims\magick.exe",
            r"C:\software\scoop\apps\ImageMagick\current\magick.exe",
        ):
            if os.path.isfile(path):
                return path
    if name == "ffmpeg":
        for path in (r"C:\software\scoop\shims\ffmpeg.exe",):
            if os.path.isfile(path):
                return path
    return None


def webp_to_png(data):
    """minihtml displays PNG, JPEG and GIF. WebP is converted when a tool exists."""
    if not data or data[8:12] != b"WEBP":
        return None
    commands = []
    magick = _converter_exe("magick")
    if magick:
        commands.append([magick, "webp:-", "png:-"])
    ffmpeg = _converter_exe("ffmpeg")
    if ffmpeg:
        commands.append([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-f", "image2", "-vcodec", "png", "pipe:1",
        ])
    run_kwargs = {
        "input": data,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 20,
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, **run_kwargs)
        except Exception as e:
            debug("webp convert failed", cmd[0], e)
            continue
        if proc.returncode == 0 and proc.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            return proc.stdout
    return None


def fetch_url(url, limit=1500000):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    })
    chunks = []
    total = 0
    with urllib.request.urlopen(req, timeout=8) as resp:
        while total < limit:
            block = resp.read(65536)
            if not block:
                break
            chunks.append(block)
            total += len(block)
    if total >= limit:
        raise ValueError("image too large")
    return b"".join(chunks)


def load_remote_images(view_id, generation, pending, max_width):
    """Download remote images off the UI thread, then insert phantoms."""
    for rel_p, scale, line_b, key in pending:
        if ImageHandler.generation.get(view_id) != generation:
            return
        try:
            data = fetch_url(rel_p)
            data = webp_to_png(data) or data
            w, h, ttype = get_image_size(io.BytesIO(data))
        except Exception as e:
            debug("Failed to load URL {}:".format(rel_p), e)
            continue
        if not ttype or not w or not h:
            continue
        if scale:
            w *= scale
            h *= scale
        if max_width and w > max_width:
            m = max_width / w
            h *= m
            w = max_width
        b64_data = base64.encodebytes(data).decode('ascii').replace('\n', '')
        html = u'<img src="data:image/{};base64,{}" class="centerImage" width="{}" height="{}">'.format(
            ttype, b64_data, int(w), int(h))
        sublime.set_timeout(lambda view_id=view_id, generation=generation, key=key,
                            line_b=line_b, html=html, rel_p=rel_p, data=data:
                            _apply_remote_phantom(view_id, generation, key, line_b, html, rel_p, data), 0)


def _apply_remote_phantom(view_id, generation, key, line_b, html, rel_p, data):
    if ImageHandler.generation.get(view_id) != generation:
        return
    view = sublime.View(view_id)
    if not view.is_valid():
        return
    phantom = (key, html)
    view.add_phantom(key, sublime.Region(line_b), html, sublime.LAYOUT_BLOCK)
    ImageHandler.phantoms[view_id].add(phantom)
    ImageHandler.urldata[view_id][rel_p] = data


def get_image_size(f):
    """
    Determine the image type of img and return its size.
    """
    head = f.read(32)
    ttype = None

    debug(str(head))
    debug(str(head[:4]))
    debug(head[:4] == b'<svg')

    if _image_type(head) == 'png':
        debug('detected png')
        ttype = "png"
        check = struct.unpack('>i', head[4:8])[0]
        if check != 0x0d0a1a0a:
            return None, None, ttype
        width, height = struct.unpack('>ii', head[16:24])
    elif _image_type(head) == 'gif':
        debug('detected gif')
        ttype = "gif"
        width, height = struct.unpack('<HH', head[6:10])
    elif _image_type(head) == 'jpeg':
        debug('detected jpeg')
        ttype = "jpeg"
        try:
            f.seek(0)  # Read 0xff next
            size = 2
            ftype = 0
            while not 0xc0 <= ftype <= 0xcf:
                f.seek(size, 1)
                byte = f.read(1)
                while ord(byte) == 0xff:
                    byte = f.read(1)
                ftype = ord(byte)
                size = struct.unpack('>H', f.read(2))[0] - 2
            # SOFn block
            f.seek(1, 1)  # skip precision byte.
            height, width = struct.unpack('>HH', f.read(4))
        except Exception as e:
            debug("determining jpeg image size failed", e)
            return None, None, ttype
    elif _image_type(head) == 'webp':
        debug('detected webp')
        ttype = "webp"
        size = _webp_size(head)
        if not size:
            return None, None, ttype
        width, height = size
    elif head[:4] == b'<svg':
        debug('detected svg')
        # SVG is not rendered by ST3 in phantoms.
        # The SVG would need to be rendered as png/jpg separately, and its data
        # placed into the phantom
        return None, None, None
    else:
        debug('unable to detect image')
        return None, None, None
    return width, height, ttype


def get_path_for(view):
    """
    Returns the path of the current file in view.
    Returns / if no path is found
    """
    if view.file_name():
        return os.path.dirname(view.file_name())
    if view.window().project_file_name():
        return os.path.dirname(view.window().project_file_name())
    return '/'


class MarkdownImagesShowCommand(sublime_plugin.TextCommand):
    """
    Show local images inline.
    """

    def run(self, edit, **kwargs):
        settings = get_settings()
        max_width = settings.get('img_maxwidth', None)
        show_local = kwargs.get('show_local', True)
        show_remote = kwargs.get('show_remote', False)
        base_path = settings.get('base_path', None)
        ImageHandler.show_images(self.view,
                                 show_local=show_local,
                                 show_remote=show_remote,
                                 max_width=max_width,
                                 base_path=base_path)


class MarkdownImagesHideCommand(sublime_plugin.TextCommand):
    """
    Hide all shown images.
    """

    def run(self, edit):
        ImageHandler.hide_images(self.view)
