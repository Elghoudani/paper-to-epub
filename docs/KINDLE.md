# Notes on Kindle

What the target device actually does, and which of it drove decisions in the
code.

## The panel

The 2024 basic Kindle is 6 inches, 1072 × 1448 pixels, 300 PPI, and renders
**sixteen levels of grey**. The Paperwhite is 7 inches and 1264 × 1680 at the
same density.

Consequences:

- Colour is bytes the device throws away, so images are converted to grey by
  default (`IMAGE_GRAYSCALE`).
- An image wider than the panel is downscaled *by the reader*, with a cheaper
  filter than Pillow's, after the file has already cost you the bytes. Ship
  images at roughly the panel size with a little headroom for zoom.
- Sixteen grey levels is not many. Contrast enhancement that crushes shadows
  removes texture that cannot come back — which is why photographs and line art
  are treated differently.

## Getting a file onto the device

Use [Send to Kindle](https://www.amazon.com/sendtokindle): email, the web
uploader, or the desktop app. It converts the EPUB to Amazon's own format
server-side and syncs it to the device.

Copying an EPUB over USB is unreliable — some firmware versions open it, others
ignore the file. Send to Kindle is the path that always works.

The per-image ceiling is 5 MB, which nothing here approaches.

## What the conversion drops

Assume anything clever in CSS does not survive:

- `prefers-color-scheme` and any dark-mode rules
- CSS filters
- `image-rendering`, `text-rendering`
- `border-radius` and most decoration

This is why the stylesheet in `config.py` is short. Rules that only ever worked
in a desktop preview app are noise in the shipped file. Two things are left to
the reader on purpose: the body font, since the device's font picker overrides
it anyway, and justification, since the device has the hyphenation dictionary
and does a better job than a stylesheet can.

## Things worth checking on a real device

Measurements in the EPUB are not measurements on the panel. If you are changing
the image or CSS path, verify on hardware:

- Do superscripts survive the conversion?
- Do rotated wide tables land the right way up?
- Do white-background formula crops glow when the reader uses dark mode?
- Are equations sized to sit level with body text at the reader's font size?
