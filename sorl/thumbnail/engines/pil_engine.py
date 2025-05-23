from io import BytesIO

from sorl.thumbnail.engines.base import EngineBase

try:
    from PIL import Image, ImageDraw, ImageFile, ImageMode
    from PIL.ImageFilter import GaussianBlur
except ImportError:
    import Image
    import ImageDraw
    import ImageFile
    import ImageMode

if hasattr(Image, 'Resampling'):
    ANTIALIAS = Image.Resampling.LANCZOS
else:
    ANTIALIAS = Image.ANTIALIAS
# Image.Transpose added in 9.1.0
if hasattr(Image, 'Transpose'):
    FLIP_LEFT_RIGHT = Image.Transpose.FLIP_LEFT_RIGHT
    FLIP_TOP_BOTTOM = Image.Transpose.FLIP_TOP_BOTTOM
else:
    FLIP_LEFT_RIGHT = Image.FLIP_LEFT_RIGHT
    FLIP_TOP_BOTTOM = Image.FLIP_TOP_BOTTOM
EXIF_ORIENTATION = 0x0112


def color_count(image):
    """ Return the number of color values in the input image --
        this is the number of pixels times the band count
        of the image.
    """
    mode_descriptor = ImageMode.getmode(image.mode)
    width, height = image.size
    return width * height * len(mode_descriptor.bands)


def histogram_entropy_py(image):
    """ Calculate the entropy of an images' histogram. """
    from math import fsum, log2
    histosum = float(color_count(image))
    histonorm = (histocol / histosum for histocol in image.histogram())
    return -fsum(p * log2(p) for p in histonorm if p != 0.0)


# Select the Pillow native histogram entropy function - if
# available - and fall back to the Python implementation:
histogram_entropy = getattr(Image.Image, 'entropy', histogram_entropy_py)


def round_corner(radius, fill):
    """Draw a round corner"""
    corner = Image.new('L', (radius, radius), 0)  # (0, 0, 0, 0))
    draw = ImageDraw.Draw(corner)
    draw.pieslice((0, 0, radius * 2, radius * 2), 180, 270, fill=fill)
    return corner


def round_rectangle(size, radius, fill):
    """Draw a rounded rectangle"""
    width, height = size
    rectangle = Image.new('L', size, 255)  # fill
    corner = round_corner(radius, 255)  # fill
    rectangle.paste(corner, (0, 0))
    rectangle.paste(corner.rotate(90),
                    (0, height - radius))  # Rotate the corner and paste it
    rectangle.paste(corner.rotate(180), (width - radius, height - radius))
    rectangle.paste(corner.rotate(270), (width - radius, 0))
    return rectangle


class Engine(EngineBase):
    def get_image(self, source):
        buffer = BytesIO(source.read())
        return Image.open(buffer)

    def get_image_size(self, image):
        return image.size

    def get_image_info(self, image):
        return image.info or {}

    def is_valid_image(self, raw_data):
        buffer = BytesIO(raw_data)
        try:
            trial_image = Image.open(buffer)
            trial_image.verify()
        except Exception:
            return False
        return True

    def colorspace(self, image, geometry, options):
        """
        Wrapper for ``_colorspace``
        """
        colorspace = options['colorspace']
        format = options['format']

        return self._colorspace(image, colorspace, format)

    def _cropbox(self, image, x, y, x2, y2):
        return image.crop((x, y, x2, y2))

    def _get_exif_orientation(self, image):
        try:
            exif = image._getexif()
        except Exception:
            exif = None

        if exif:
            return exif.get(EXIF_ORIENTATION)
        else:
            return None

    def _orientation(self, image):
        orientation = self._get_exif_orientation(image)

        if orientation:
            if orientation == 2:
                image = image.transpose(FLIP_LEFT_RIGHT)
            elif orientation == 3:
                image = image.rotate(180)
            elif orientation == 4:
                image = image.transpose(FLIP_TOP_BOTTOM)
            elif orientation == 5:
                image = image.rotate(-90, expand=1).transpose(FLIP_LEFT_RIGHT)
            elif orientation == 6:
                image = image.rotate(-90, expand=1)
            elif orientation == 7:
                image = image.rotate(90, expand=1).transpose(FLIP_LEFT_RIGHT)
            elif orientation == 8:
                image = image.rotate(90, expand=1)

        return image

    def _flip_dimensions(self, image):
        orientation = self._get_exif_orientation(image)
        return orientation and orientation in [5, 6, 7, 8]

    def _colorspace(self, image, colorspace, format):
        if colorspace == 'RGB':
            # Pillow JPEG doesn't allow RGBA anymore. It was converted to RGB before.
            if image.mode == 'RGBA' and format != 'JPEG':
                return image  # RGBA is just RGB + Alpha
            if image.mode == 'LA' or (
                image.mode == 'P' and 'transparency' in image.info and format != 'JPEG'
            ):
                newimage = image.convert('RGBA')
                transparency = image.info.get('transparency')
                if transparency is not None:
                    mask = image.convert('RGBA').split()[-1]
                    newimage.putalpha(mask)
                return newimage
            return image.convert('RGB')
        if colorspace == 'GRAY':
            return image.convert('L')
        return image

    def _remove_border(self, image, image_width, image_height):
        borders = {
            'top': lambda iy, dy, y: (dy, dy + y),
            'right': lambda ix, dx, x: (ix - dx - x, ix - dx),
            'bottom': lambda iy, dy, y: (iy - dy - y, iy - dy),
            'left': lambda ix, dx, x: (dx, dx + x),
        }

        offset = {'top': 0, 'right': 0, 'bottom': 0, 'left': 0, }

        for border in ['top', 'bottom']:
            # Don't remove too much, the image may just be plain
            while offset[border] < image_height / 3.5:
                slice_size = min(image_width / 20, 10)
                y_range = borders[border](image_height, offset[border], slice_size)
                section = image.crop((0, y_range[0], image_width, y_range[1]))
                # If this section is below the threshold; remove it
                if self._get_image_entropy(section) < 2.0:
                    offset[border] += slice_size
                else:
                    break

        for border in ['left', 'right']:
            while offset[border] < image_width / 3.5:
                slice_size = min(image_height / 20, 10)
                x_range = borders[border](image_width, offset[border], slice_size)
                section = image.crop((x_range[0], 0, x_range[1], image_height))
                if self._get_image_entropy(section) < 2.0:
                    offset[border] += slice_size
                else:
                    break

        return image.crop((offset['left'], offset['top'], image_width - offset['right'],
                           image_height - offset['bottom']))

    # Credit to chrisopherhan https://github.com/christopherhan/pycrop
    # This is just a slight rework of pycrops implimentation
    def _entropy_crop(self, image, geometry_width, geometry_height, image_width, image_height):
        geometry_ratio = geometry_width / geometry_height

        # The is proportionally wider than it should be
        while image_width / image_height > geometry_ratio:

            slice_width = max(image_width - geometry_width, 10)

            right = image.crop((image_width - slice_width, 0, image_width, image_height))
            left = image.crop((0, 0, slice_width, image_height))

            if self._get_image_entropy(left) < self._get_image_entropy(right):
                image = image.crop((slice_width, 0, image_width, image_height))
            else:
                image = image.crop((0, 0, image_height - slice_width, image_height))

            image_width -= slice_width

        # The image is proportionally taller than it should be
        while image_width / image_height < geometry_ratio:

            slice_height = min(image_height - geometry_height, 10)

            bottom = image.crop((0, image_height - slice_height, image_width, image_height))
            top = image.crop((0, 0, image_width, slice_height))

            if self._get_image_entropy(bottom) < self._get_image_entropy(top):
                image = image.crop((0, 0, image_width, image_height - slice_height))
            else:
                image = image.crop((0, slice_height, image_width, image_height))

            image_height -= slice_height

        return image

    # Add the histogram_entropy fumnction as a static method:
    _get_image_entropy = staticmethod(histogram_entropy)

    def _scale(self, image, width, height):
        return image.resize((width, height), resample=ANTIALIAS)

    def _crop(self, image, width, height, x_offset, y_offset):
        return image.crop((x_offset, y_offset,
                           width + x_offset, height + y_offset))

    def _rounded(self, image, r):
        i = round_rectangle(image.size, r, "notusedblack")
        image.putalpha(i)
        return image

    def _blur(self, image, radius):
        return image.filter(GaussianBlur(radius))

    def _padding(self, image, geometry, options):
        x_image, y_image = self.get_image_size(image)
        left = int((geometry[0] - x_image) / 2)
        top = int((geometry[1] - y_image) / 2)
        color = options.get('padding_color')
        im = Image.new(image.mode, geometry, color)
        im.paste(image, (left, top))
        return im

    def _get_raw_data(self, image, format_, quality, image_info=None, progressive=False):
        # Increase (but never decrease) PIL buffer size
        ImageFile.MAXBLOCK = max(ImageFile.MAXBLOCK, image.size[0] * image.size[1])
        bf = BytesIO()

        params = {
            'format': format_,
            'quality': quality,
            'optimize': 1,
        }

        # keeps icc_profile
        if image_info and 'icc_profile' in image_info:
            params['icc_profile'] = image_info['icc_profile']

        raw_data = None

        if format_ == 'JPEG' and progressive:
            params['progressive'] = True
        try:
            # Do not save unnecessary exif data for smaller thumbnail size
            params.pop('exif', {})
            image.save(bf, **params)
            raw_data = bf.getvalue()
        except OSError:
            # Try without optimization and icc_profile.
            params.pop('optimize')
            if 'icc_profile' in params:
                params.pop('icc_profile')
            image.save(bf, **params)
            raw_data = bf.getvalue()
        finally:
            bf.close()

        return raw_data
