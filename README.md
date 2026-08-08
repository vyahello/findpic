<p align="center">
  <img src="docs/logo.svg" alt="findpic" width="520">
</p>

<p align="center">
  <a href="https://github.com/vyahello/findpic/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/vyahello/findpic/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776ab">
  <img alt="exiftool" src="https://img.shields.io/badge/exiftool-13.x-0b7285">
  <img alt="Languages" src="https://img.shields.io/badge/languages-EN%20%C2%B7%20UK-5eead4">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue">
</p>

---

Point it at a photo and get a straight answer: **what took it, when, where, whether it is an untouched original, and what it gives away about you.**

Built on [`exiftool`](https://exiftool.org/). findpic does not replace it — it reads what exiftool extracts and turns 180 cryptic tags into a page you can act on. Output is available in **English and Ukrainian**.

## What it looks like

Real output, unedited, from a demo photo carrying public landmark coordinates:

```console
$ findpic demo.jpg
```
```
╭──────────────────────────────────────────────────────────────────────────────────────╮
│  findpic  ·  demo.jpg                                                                │
│  JPEG · 217.4 KB · 92 metadata tags                                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯

  !   Originality      MODIFIED              This file has been edited or re-saved since
                                             it was taken.
  x   Privacy          HIGH EXPOSURE         Sharing this file as-is hands over your
                                             location, device, or identity.
  +   Structure        CLEAN                 The file structure is exactly what a normal
                                             image looks like.

 DEVICE
 Camera         Apple iPhone 15 Pro
 System         iOS 17.5.1
 Lens           iPhone 15 Pro back triple camera 6.765mm f/1.78  (24 mm equivalent)
 Owner          Demo Owner
 Body serial    DEMO-SN-0001

 WHEN
 Taken          2024-07-14 19:42:08 +02:00   (Sunday evening)
 Time zone      UTC+02:00  (from the camera's own offset tag)
 Modified       unchanged since capture
 GPS clock      2024-07-14 UTC

 WHERE
 Coordinates    48.858370, 2.294481   ±6 m
 DMS            48°51'30.13"N 2°17'40.13"E
 Place          Avenue Gustave Eiffel, Paris, Ile-de-France, France
 Altitude       38.4 m above sea level
 Facing         291° WNW  (relative to true north)
 Movement       stationary at the moment of capture
 Map            open in OpenStreetMap

 IMAGE
 Dimensions     4032 × 3024   (12.2 MP)
 Exposure       ISO 64 · f/1.78 · 1/120 s · 6.765 mm
 Flash          Off, Did not fire
 Encoding       Baseline DCT, Huffman coding

 FINDINGS
 Originality
  ! Re-encoded by a general-purpose JPEG library, not by the camera  (low confidence)
    The compression tables belong to libjpeg, which ImageMagick, Pillow, GIMP and most
    website upload pipelines use. It proves the pixels were compressed again after the
    camera wrote them, but it does not say which program did it. Estimated quality
    setting: 92.

  - Claims to be Apple iPhone 15 Pro but the vendor's private data block is gone
    (medium confidence)
    Cameras write a proprietary block full of internal settings. Almost nothing preserves
    it except the original file, so its absence means the file was re-saved — or the
    camera name was written in by hand.

 Privacy
  x Exact location is embedded: Avenue Gustave Eiffel, Paris, Ile-de-France, France
    Anyone who receives this file can read the coordinates 48.858370, 2.294481 straight
    out of it. The camera rated the fix accurate to about 6 metres. If this is your home,
    your workplace, or anywhere you go regularly, that is now known to every recipient.
    fix: exiftool -gps:all= -xmp:geotag= -o clean_copy.jpg photo.jpg

  ! The file names a person: Artist: Demo Owner
    These fields are usually filled in once, in a camera's setup menu or an editor's
    preferences, and then quietly attach to every photo afterwards.
    fix: exiftool -artist= -copyright= -ownername= -xmp:creator= -iptc:all= -o
    clean_copy.jpg photo.jpg

  - The location record also includes altitude 38 m, camera bearing 291°
    Beyond the coordinates, the file records which way you were facing and how high you
    were — enough to work out which window, which floor, or which direction you were
    travelling.

 FILE
 SHA-256        7d999770e004ee96f6c9cc5db292c7718a1bb3efd6313753f9470897f733f518
 MD5            61a65d7ac2e4831eb0721c5c85272131
 MIME           image/jpeg
```

Every finding is a sentence, not a tag dump — and every privacy finding ends with the exact command that removes that leak.

### Українською

`--lang uk` translates the whole report. Shell commands are never translated, because a translated command would not run.

```console
$ findpic demo.jpg --lang uk
```
```
╭──────────────────────────────────────────────────────────────────────────────────────╮
│  findpic  ·  demo.jpg                                                                │
│  JPEG · 217.4 КБ · 92 теги метаданих                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────╯

  !   Оригінальність   ЗМІНЕНО               Цей файл редагували або перезберігали після
                                             зйомки.
  x   Приватність      ВИСОКИЙ РИЗИК         Надіславши цей файл як є, ви передаєте своє
                                             місцеперебування, пристрій або особу.
  +   Структура        ЧИСТО                 Структура файлу саме така, як у звичайного
                                             зображення.

 ПРИСТРІЙ
 Камера         Apple iPhone 15 Pro
 Система        iOS 17.5.1
 Об'єктив       iPhone 15 Pro back triple camera 6.765mm f/1.78  (еквівалент 24 mm)

 КОЛИ
 Знято          2024-07-14 19:42:08 +02:00   (неділя, вечір)
 Часовий пояс   UTC+02:00  (з власного тега камери)
 Змінено        не змінювався від моменту зйомки

 ДЕ
 Координати     48.858370, 2.294481   ±6 м
 Місце          Avenue Gustave Eiffel, Париж, Іль-де-Франс, Франція
 Напрямок       291° ЗПнЗ  (відносно істинної півночі)
 Рух            нерухомо в момент зйомки

 ЗНАХІДКИ
 Приватність
  x У файл вбудовано точне місце: Avenue Gustave Eiffel, Париж, Іль-де-Франс, Франція
    Будь-хто, хто отримає цей файл, прочитає з нього координати 48.858370, 2.294481.
    Камера оцінила точність приблизно в 6 метрів. Якщо це ваш дім, робота чи місце, де ви
    буваєте регулярно, тепер про це знає кожен отримувач.
    як прибрати: exiftool -gps:all= -xmp:geotag= -o clean_copy.jpg photo.jpg
```

Ukrainian plurals decline properly — `1 обличчя`, `2 обличчя`, `5 облич` — because a tool that gets that wrong reads as machine-translated.

### A whole folder at a glance

```console
$ findpic ~/Pictures/*.jpg --summary
```
```
!x+  demo.jpg                   Apple iPhone 15 Pro  2024-07-14 19:42  Avenue Gustave Eiffel, Pa
!~+  IMG_4417.JPG               Apple iPhone 15 Pro  2024-07-14 19:42  no location
+x!  poly.jpg                   Apple iPhone X       2021-02-27 22:23  Lviv, Ukraine
?++  naked.jpg                  Unknown device       no timestamp      no location
^^^
originality, privacy, structure  →  + good   ~ fair   ! poor   x bad   ? unknown
```

## Three verdicts, deliberately separate

Most tools collapse everything into one score. That destroys the information you actually want, because the axes are independent:

| Axis | Question | Bands |
|---|---|---|
| **Originality** | Has this been edited or re-saved since capture? | `ORIGINAL` · `LIKELY ORIGINAL` · `MODIFIED` · `INCONSISTENT` · `UNKNOWN` |
| **Privacy** | What does it give away about you? | `CLEAN` · `LOW` · `MODERATE` · `HIGH EXPOSURE` |
| **Structure** | Is anything hiding in the file? | `CLEAN` · `MINOR ANOMALIES` · `SUSPICIOUS` · `HIGH RISK` |

A holiday photo straight off your phone is a perfect original *and* a serious privacy leak. A scrubbed meme is a privacy non-event *and* completely unverifiable. One number cannot say that.

**`UNKNOWN` is a real answer.** A file with no metadata gets `UNKNOWN` for originality — never "suspicious". Absence of evidence is reported as absence of evidence.

## Install

Requires Python 3.10+ and exiftool.

```bash
sudo apt install libimage-exiftool-perl     # Debian / Kali / Ubuntu
brew install exiftool                       # macOS

git clone https://github.com/vyahello/findpic && cd findpic
pip install -e .
```

## Use

```bash
findpic photo.jpg                  # full report
findpic photo.jpg --lang uk        # звіт українською
findpic *.jpg --summary            # one line per file
findpic album/ --recursive         # walk a directory
findpic photo.jpg --json           # machine-readable
findpic photo.jpg --json --raw     # …including every raw tag
findpic photo.jpg --quiet          # hide informational findings
findpic photo.jpg --no-geocode     # never touch the network
```

Language is taken from `FINDPIC_LANG`, then your locale, then English. `--lang` overrides.

Exit codes, so it composes in scripts: `0` nothing notable · `1` notable findings · `2` error.

In `--json`, `id` is stable across languages and `title`/`detail` follow `--lang`, so scripts key on the id and humans read the prose.

## What it looks for

**Device and capture** — make, model, OS version, lens, capture mode (portrait, Live Photo, HDR+), and how long the phone had been switched on. Apple, Samsung, Google, Xiaomi, Canon, Nikon, Sony, GoPro, DJI and more.

**Originality** — editor signatures in `Software`; **JPEG quantization-table fingerprints**, which identify the encoder even when every Exif tag looks pristine; XMP edit history and `DerivedFrom` chains; `ModifyDate` diverging from `DateTimeOriginal`; crop-versus-resize detected by comparing Exif's remembered dimensions against the real ones; the Adobe APP14 marker; progressive encoding a camera would not produce; missing MakerNotes; camera claims too thin to be genuine; and a GPS clock that disagrees with the camera clock.

**Privacy** — coordinates (with accuracy radius, altitude, bearing and speed), written place names, names/emails/copyright, body and lens serial numbers, per-device UUIDs that link your photos to each other, face regions and tagged people, keywords, captions, your time zone, and device uptime. Each with the exact command that removes it.

**Structure** — data appended past the image's end marker, format/extension mismatches, code-like content in metadata fields, right-to-left filename spoofing, double extensions, oversized fields, and decompression-bomb ratios.

**Provenance** — C2PA Content Credentials, IPTC `DigitalSourceType`, AI generator signatures, Stable Diffusion parameter blocks, and messenger/social-platform fingerprints.

## Honest limits

Worth being blunt about, because a tool that overclaims is worse than none:

- **findpic cannot tell you whether an image is AI-generated.** It can tell you when a file *declares* that it is. A stripped AI image and a stripped photograph are identical to anything that only reads metadata.
- **It cannot prove a photo is genuine.** Every tag it reads can be forged with the same exiftool it uses. Consistent metadata means "nothing contradicts itself", not "this is real".
- **It cannot separate "stripped by a platform" from "stripped deliberately".** Both look the same.
- **It does not verify C2PA signatures.** It reports that a manifest exists. Use [`c2patool`](https://github.com/contentauth/c2patool) to check one.
- **It does not detect steganography** and does not pretend to. It flags structural anomalies and points you at a dedicated tool.
- **It never decodes pixels.** Everything comes from headers and metadata, which is what keeps it safe to point at untrusted files.
- **Enum values exiftool decodes** (`Flash`, `Orientation`, `Program`) stay in exiftool's canonical English. findpic translates its own prose, not exiftool's vocabulary.

## Safety

findpic is built to be pointed at files you do not trust.

- **Read-only.** It never writes to, moves, or modifies the images you give it.
- **No shell.** exiftool is invoked with an argv list, never `shell=True`. Paths are resolved to absolute, so a filename starting with `-` cannot become an option and shell metacharacters are inert. There are tests for exactly this.
- **Bounded.** Per-file timeout and a file-size ceiling.
- **No pixel decoding**, so a decompression bomb costs a few seeks.
- **One network call, and only one.** Reverse geocoding sends coordinates to OpenStreetMap's Nominatim, cached and rate-limited to their policy. `--no-geocode` makes the tool completely offline — a test asserts no socket is opened.

## Development

```bash
pip install -e ".[dev]"
pytest                      # full suite
pytest -m "not samples"     # skip tests needing real photos in samples/
```

Fixture images are generated at test time from ImageMagick and exiftool, so no binaries — and no one's real photographs — live in the repository.

### Adding a rule

A rule produces facts, never sentences:

```python
@rule("my_check", Category.PRIVACY)
def my_check(context: Context) -> Iterable[Finding]:
    if not context.meta.has("Some:Tag"):
        return
    yield Finding(
        id="privacy.my_check",
        category=Category.PRIVACY,
        severity=Severity.WARNING,
        confidence=Confidence.HIGH,
        params={"value": context.meta.str("Some:Tag")},
        weight=15,
        remediation="exiftool -some:tag= -o clean_copy.jpg photo.jpg",
    )
```

Then add `finding.privacy.my_check.title` and `.detail` to `locales/en.json` and `locales/uk.json`. The test suite fails if a key exists in one catalogue and not the other, so a half-translated release cannot ship.

A rule that raises is caught and reported as a findpic bug — it never costs you the rest of the report.

### Adding a language

Copy `src/findpic/locales/en.json`, translate the values, and add a plural rule to `PLURAL_RULES` in `i18n.py` if the language needs more than two forms. Nothing else changes.

## Layout

```
src/findpic/
  exif.py          hardened exiftool subprocess wrapper
  container.py     JPEG/PNG structure walker (finds appended data)
  geocode.py       Nominatim reverse geocoding, cached and rate-limited
  i18n.py          catalogue loading, plural rules, translated units
  locales/         en.json, uk.json
  models.py        Report, Finding, Verdict — the only thing renderers see
  tables.py        editors, AI generators, filename patterns, encoder digests
  analysis/
    extract.py     raw tags  →  structured fields
    registry.py    the @rule decorator
    rules_*.py     authenticity, privacy, structure, ai, platform
    verdict.py     the three-axis scoring model
  render/
    terminal.py    the rich report
```

## Licence

MIT.
