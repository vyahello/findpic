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

Real output, unedited. The demo photo is built by **`python scripts/make-demo.py`** — synthetic pixels, Eiffel Tower coordinates, an owner called *Demo Owner*. A privacy tool should not demonstrate itself on somebody's home, and a sample nobody can regenerate is a sample nobody can check.

```console
$ findpic demo.jpg
```
```
╭────────────────────────────────────────────────────────────────────────────────────────╮
│  findpic  ·  demo.jpg                                                                  │
│  JPEG · 238.5 KB · 94 metadata tags                                                    │
╰────────────────────────────────────────────────────────────────────────────────────────╯

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
 GPS clock      2024-07-14 17:42:08 UTC
 File saved     2024-07-14 20:42:08 +03:00

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
 Colour         Uncalibrated
 Encoding       Baseline DCT, Huffman coding

 FINDINGS
 Originality
  ! Re-encoded by a general-purpose JPEG library, not by the camera  (low confidence)
    The compression tables belong to libjpeg, which ImageMagick, Pillow, GIMP and most
    website upload pipelines use. It proves the pixels were compressed again after the
    camera wrote them, but it does not say which program did it. Estimated quality
    setting: 92.

  - Claims to be Apple iPhone 15 Pro but the vendor's private data block is gone  (medium
    confidence)
    Cameras write a proprietary block full of internal settings. Almost nothing preserves
    it except the original file, so its absence means the file was re-saved — or the
    camera name was written in by hand.

 Privacy
  x Exact location is embedded: Avenue Gustave Eiffel, Paris, Ile-de-France, France
    Anyone who receives this file can read the coordinates 48.858370, 2.294481 straight
    out of it. The camera rated the fix accurate to about 6 metres. If this is your home,
    your workplace, or anywhere you go regularly, that is now known to every recipient.
    fix: exiftool -gps:all= -xmp:geotag= -o clean_copy.jpg photo.jpg

  ! 2 identifiers that tie this photo to one specific device
    Values like Camera body serial number, Per-image unique ID are stable across photos.
    Anyone holding two of your pictures can prove they came from the same camera, even if
    nothing else matches.
    fix: exiftool -serialnumber= -lensserialnumber= -imageuniqueid= -makernotes:all= -o
    clean_copy.jpg photo.jpg

  ! The file names a person: Artist: Demo Owner, Camera owner: Demo Owner
    These fields are usually filled in once, in a camera's setup menu or an editor's
    preferences, and then quietly attach to every photo afterwards.
    fix: exiftool -artist= -copyright= -ownername= -xmp:creator= -iptc:all= -o
    clean_copy.jpg photo.jpg

  - The location record also includes altitude 38 m, camera bearing 291°
    Beyond the coordinates, the file records which way you were facing and how high you
    were — enough to work out which window, which floor, or which direction you were
    travelling.

  i The time zone you were in is recorded (UTC+02:00)
    Even with coordinates removed, the offset narrows you to a band of the world, and
    across several photos it maps out your travel.
    fix: exiftool -offsettime*= -o clean_copy.jpg photo.jpg

 FILE
 SHA-256        e43e23e337a2194e8b0e217f9a497d3aa97b7ce0a4d41352ca56e59c97bcebda
 MD5            6725f2967f4c8885ec8b91e27c838e76
 MIME           image/jpeg
```

Every finding is a sentence, not a tag dump — and every privacy finding ends with the exact command that removes that leak.

### Українською

`--lang uk` translates the whole report. Shell commands are never translated, because a translated command would not run.

```console
$ findpic demo.jpg --lang uk
```
```
╭────────────────────────────────────────────────────────────────────────────────────────╮
│  findpic  ·  demo.jpg                                                                  │
│  JPEG · 238.5 КБ · 94 теги метаданих                                                   │
╰────────────────────────────────────────────────────────────────────────────────────────╯

  !   Оригінальність   ЗМІНЕНО               Цей файл редагували або перезберігали після
                                             зйомки.
  x   Приватність      СЕРЙОЗНИЙ ВИТІК       Надіславши цей файл як є, ви видаєте, де
                                             були, чим знімали і хто ви.
  +   Структура        ЧИСТО                 Структура файлу саме така, як у звичайного
                                             зображення.

 ПРИСТРІЙ
 Камера         Apple iPhone 15 Pro
 Система        iOS 17.5.1
 Об'єктив       iPhone 15 Pro back triple camera 6.765mm f/1.78  (еквівалент 24 мм)
 Власник        Demo Owner
 Серійний №     DEMO-SN-0001

 КОЛИ
 Знято          2024-07-14 19:42:08 +02:00   (неділя, вечір)
 Часовий пояс   UTC+02:00  (з власного тега камери)
 Змінено        не змінювався після зйомки
 Годинник GPS   2024-07-14 17:42:08 UTC
 Збережено      2024-07-14 20:42:08 +03:00

 ДЕ
 Координати     48.858370, 2.294481   ±6 м
 Градуси        48°51'30.13"N 2°17'40.13"E
 Місце          Avenue Gustave Eiffel, Париж, Іль-де-Франс, Франція
 Висота         38.4 м над рівнем моря
 Напрямок       291° ЗхПнЗх  (відносно істинної півночі)
 Рух            нерухомо в момент зйомки
 Карта          відкрити в OpenStreetMap

 ЗОБРАЖЕННЯ
 Розміри        4032 × 3024   (12.2 Мп)
 Експозиція     ISO 64 · f/1.78 · 1/120 с · 6.765 мм
 Спалах         Off, Did not fire
 Колір          Uncalibrated
 Кодування      Baseline DCT, Huffman coding

 ЗНАХІДКИ
 Оригінальність
  ! Перекодовано універсальною бібліотекою JPEG, а не камерою  (впевненість: низька)
    Таблиці стиснення належать libjpeg — її використовують ImageMagick, Pillow, GIMP і
    майже всі сайти, куди завантажують фото. Це доводить, що пікселі стиснули ще раз після
    того, як їх записала камера, але не вказує, яка саме програма це зробила. Приблизна
    якість стиснення: 92.

  - Заявлено Apple iPhone 15 Pro, але службовий блок виробника зник  (впевненість:
    середня)
    Камери записують службовий блок із внутрішніми налаштуваннями. Майже жодна програма
    його не зберігає, тож якщо блоку немає — файл перезберігали або назву камери вписали
    вручну.

 Приватність
  x У файлі записано точне місце зйомки: Avenue Gustave Eiffel, Париж, Іль-де-Франс,
    Франція
    Координати 48.858370, 2.294481 прочитає з цього файлу будь-хто, кому ви його
    надішлете. Камера оцінила похибку приблизно в 6 м. Якщо це ваш дім, робота чи місце,
    де ви буваєте регулярно, — ви віддаєте цю адресу разом зі знімком.
    як прибрати: exiftool -gps:all= -xmp:geotag= -o clean_copy.jpg photo.jpg
```

Ukrainian plurals decline properly — `1 обличчя`, `2 обличчя`, `5 облич` — because a tool that gets that wrong reads as machine-translated.

### A whole folder at a glance

```console
$ findpic *.jpg --summary
```
```
!x+  demo.jpg                   Apple iPhone 15 Pro  2024-07-14 19:42  Avenue Gustave Eiff
!~+  d2.jpg                     Apple iPhone 15 Pro  2024-07-14 19:42  no location
?++  naked.jpg                  Unknown device       no timestamp      no location
^^^  originality, privacy, structure  →  + good  ~ fair  ! poor  x bad  ? unknown
```

The legend goes to **stderr**, so it explains the glyphs to you and stays out of whatever you pipe the rest into.

## Telegram bot

The same analysis, as a bot — in English and Ukrainian, switchable per user.

<img src="docs/bot-icon.png" alt="findpic bot" width="88" align="left" hspace="16" vspace="4">

The bot leads with the one thing that decides whether any of it works: send the
picture **as a file**, not as a photo. Telegram re-compresses photos and strips
every tag before the bot ever sees them — and an empty report reads as a broken
bot, so it says so up front rather than afterwards.

<br clear="left">

A chat message is not a terminal, so the report is shaped differently. The three
verdicts stay in the CLI; the bot leads with **originality alone**, because "is
this the picture that came out of the camera" is the one yes/no nothing else on
screen answers. Everything else is said exactly once, and every number is turned
into a claim — `GPSImgDirection 291` becomes a sentence about where the camera
was pointing.

```
🔎 demo.jpg
238.5 KB · JPEG · 4032×3024

✂️ MODIFIED
Edited, cropped or re-saved after capture.

📱 DEVICE
Apple iPhone 15 Pro · iOS 17.5.1
24 mm equiv.

🕓 WHEN
14 July 2024, 19:42:08
Sunday evening

📍 WHERE
Avenue Gustave Eiffel, Paris, Ile-de-France, France
48.858370, 2.294481
Accurate to ±6 m — to the building
38 m above sea level
The camera was pointing west-north-west (291°)
You were standing still
🗺 Open on the map

📸 THE SHOT
12.2 MP · 4032 × 3024 · 4:3
ISO 64 · f/1.78 · 1/120 s · 6.765 mm
Overcast or shade
Flash did not fire

⚠️ WHAT THIS GIVES AWAY
• The exact coordinates of where it was taken
• Identifiers shared by every photo from this device
• Owner's name: Demo Owner
```

Under it sit the buttons that do something with the answer:

| Button | What it does |
|---|---|
| 🧹 **Send me a clean copy** | The same picture back with the identifying metadata stripped, orientation and colour kept so it still displays correctly. Shown only when there is something worth removing. |
| 🔍 **Show every raw tag** | Every tag exiftool found, as a text file — including the ones the report deliberately leaves out. |
| 💾 **Backup the metadata** | An `.mie` sidecar holding everything, binary MakerNotes included, so a strip can be undone. Offered wherever the clean copy is, because losing the only record of where a photo was taken is not a thing to find out afterwards. |
| 🌐 **Language** | English ⇄ Ukrainian, remembered per user. |

A persistent keyboard under the message box carries 📖 **How to use**,
🌐 **Language**, 🔒 **Privacy** and ℹ️ **About**. `/help`, `/lang`, `/privacy`
and `/about` do the same thing — the buttons exist because slash commands are
discoverable only if you already know they are there, and tedious to type on a
phone. Captions from *every* language are accepted, so a keyboard left on screen
from before a language switch keeps working instead of being echoed back as
unrecognised text.

Setup takes about five minutes — see **[docs/BOT_SETUP.md](docs/BOT_SETUP.md)**.
Name, descriptions and command menu are set by the bot itself from the message
catalogue, so they live in git rather than in @BotFather.

Deployment is a container carrying its own pinned exiftool, deployed to a VPS by
GitHub Actions after CI goes green. It runs unprivileged and read-only, because
it parses files that arrive from strangers.

### Who is using it

`scripts/bot-stats.py` reads the bot's own database and says who has been
talking to it. Standard library only — no venv, no install, nothing to add to
the server. It reads the database out of the Docker volume through a throwaway
container, locally or over ssh, so it needs the docker group rather than root,
and it always works on a copy rather than the file the bot is writing to.

The sample below is synthetic, from a seeded database — a report about who uses
a bot should not demonstrate itself on real people's accounts.

```bash
scripts/bot-stats.py --docker                     # on the server itself
scripts/bot-stats.py --ssh you@server --ssh-port 2022    # from your laptop
scripts/bot-stats.py --docker --photos --limit 0  # every picture, one per line
scripts/bot-stats.py --docker --user @someone     # one account, in order
scripts/bot-stats.py --docker --device iphone --with-gps
scripts/bot-stats.py --docker --failed            # only what went wrong
scripts/bot-stats.py --docker --csv-photos out.csv --json
```

```
  4 accounts known · 4 of them active, over the whole history
  92 interactions · 64 pictures sent · 59 analysed · 5 turned away
  26 arrived with no camera in them

WHAT WAS SENT  59 analysed · 5 turned away · 4 shown
  when (UTC)  who          camera            os         sent  type found
  ──────────────────────────────────────────────────────────────────────
  08-27 14:30 John Doe     Google Pixel 8    Android 14 file  JPEG serial edite…
  08-27 18:42 John Doe     —                 —          photo JPEG stripped
  08-27 23:29 John Doe     —                 —          photo JPEG stripped
  08-29 20:53 Sam          · too_big — file over the size limit
  … 60 earlier not shown — --limit 0 for all

WHO
  id          name         username     lang  camera              pics  last
  1002        John Doe     @jdoe        en    Google Pixel 8       22 08-27
  1004        Sam          @lurker      en    —                    17 08-29
  1003        Марко        —            uk    samsung SM-S918B     15 08-26
  1001        Olena K      @olena_k     uk    Apple iPhone 14 P…   10 08-28★
  ★ Telegram Premium · pics counts what they sent, refusals included

DEVICES  (read out of the photos, not from Telegram)
  camera                   pics  os                    who
  Google Pixel 8             14  Android 14 (14)       John Doe (14)
  samsung SM-S918B           10  One UI 6.1 (10)       Марко (10)
  Apple iPhone 14 Pro         9  iOS 17.4.1 (9)        Olena K (9)
  (no camera in the file)    26  stripped before it reached the bot

  how they sent it  as a file 47 · as a photo 12
  file types        JPEG 44 · HEIC 15

WHAT THE PICTURES WERE
  originality       unknown 26 · good 18 · fair 10 · poor 5
  privacy           good 26 · poor 14 · fair 11 · bad 8
  structure         good 59
  most found        platform.stripped 37 · privacy.gps_location 22
  carried           22 with GPS · 11 with a serial · 26 already stripped
  buttons pressed   clean offered on 33 → tags 5 · clean 5 · backup 3
  analysis took     median 1.1 s · slowest 2.4 s
  file size         median 5.4 MB · largest 7.4 MB
  tags per photo    median 122 · most 186

WHERE THE PICTURES WERE TAKEN  (from the photo's own tags)
  countries         US 8 · PL 7 · UA 7
  places            Portland, Oregon 8 · Kraków, Lesser Poland 7 · Lviv, Lviv…
  camera clock      +02:00 19 · -05:00 14
  26 of 59 carried no capture data at all

TURNED AWAY OR BROKEN
  quota            4  hit the daily limit         Марко (2), John Doe (1), Sam …
  too_big          1  file over the size limit    Sam (1)

ARCHIVE  /archive
  64 sent · 31 kept · 18 over the size limit · 10 already had that file
  holding 126.1 MB in 31 distinct files · 52.1 MB saved by deduplication
  oldest 2026-08-06 · newest 2026-08-27
  2026-08-23 John Doe     by-date/2026-08-23/20260823T233900Z-u1002-8…    3.9 MB
  2026-08-25 Olena K      by-date/2026-08-25/20260825T074900Z-u1001-1…    6.0 MB
  2026-08-26 John Doe     by-date/2026-08-26/20260826T013300Z-u1002-0…    7.5 MB
  2026-08-26 John Doe     by-date/2026-08-26/20260826T153700Z-u1002-d…    4.5 MB
  2026-08-26 Марко        by-date/2026-08-26/20260826T180700Z-u1003-c…    2.9 MB
  2026-08-26 Sam          by-date/2026-08-26/20260826T214200Z-u1004-b…    5.2 MB
  2026-08-27 Sam          by-date/2026-08-27/20260827T095700Z-u1004-e…    2.8 MB
  2026-08-27 John Doe     by-date/2026-08-27/20260827T184200Z-u1002-c…    2.4 MB
  … 33 more kept files — --csv-photos lists them all
```

### Keeping the photos

A bot that reads photographs is more interesting to its operator if they can see
what it is being used on. `ARCHIVE_DIR` turns that on; empty — the default —
keeps nothing, because this is a decision about other people's pictures rather
than a setting.

```
archive/
  objects/3f/9a/3f9a2c1b….heic                                   the bytes, one copy
  by-date/2026-08-29/20260829T134501Z-u7332288724-3f9a2c1b.heic  a hardlink
```

A bind mount rather than a named volume, and that is the point: a named volume
lives under `/var/lib/docker` as `root:root 0700`, so looking at a single JPEG
would need `sudo` or a throwaway container. This is `ls`-able and `scp`-able
with neither. `by-date/` is what you browse — one directory per day, names that
sort chronologically and carry the moment, the sender and the first eight hex of
the digest, so `grep 3f9a2c1b` joins a file on disk to a row in the ledger to a
line in the report. The same picture sent twice is two entries and one copy of
the bytes.

**No byte of what the sender called their file ever enters a path.** Every name
is built from a timestamp, an integer user id, a hex digest and an allowlisted
extension, so traversal, NUL bytes, right-to-left overrides and 4 kB names are
impossible rather than defended against. The extension comes from the file's own
first bytes, not from the claim — a file that crashes exiftool is exactly the one
worth having on disk.

Caps on total bytes, per person, per file and free space remaining — a new
picture is refused when one is reached, rather than an old one deleted to make
room; a retention window clamped to the analytics one, because past
that the sender's name is deleted and a photograph nobody can attribute is worse
than no photograph. Nothing in the archive can fail an analysis: every outcome,
including the refusals, is a row with a state, since an archive whose failures
are invisible is worse than none.

**Turning it on rewrites `/privacy`, in both languages**, and adds `/forget` —
which deletes every picture that person sent and everything recorded about them.
The bot currently tells users "nothing is archived"; that sentence and the code
that makes it false cannot ship in either order.

**What a bot cannot know, and this does not pretend to.** The Telegram Bot API
hands over an *account* — id, name, username, the language the client asks in,
the premium flag — and the moment each message arrived. There is no device in
it, no operating system, no app version, no IP address and no location. Those
exist only in Telegram's own "Devices" screen, for your own account.

So the two columns everybody asks for first come from somewhere else, and the
report labels them:

- **Devices and OS** are read out of the photographs, by findpic. That is a
  claim about the camera, not about the phone holding the Telegram session, and
  it is blank for everyone whose photos arrived already stripped — which, for a
  bot about metadata, is most of them.
- **Where** has two weak proxies: the client language, and the hours somebody is
  active, which places their waking day on the clock. Neither is a location.

Recording is on by default and the bot's `/privacy` screen states exactly what
it keeps and for how long, in both languages, changing automatically with the
configuration — that screen is where somebody decides whether to trust the
thing, and a notice describing a different build is worse than no notice.
`ANALYTICS=0` turns it off, `ANALYTICS_RETENTION_DAYS` sets the window.
Message text is never recorded under any setting.

## When the metadata is gone

A photo that came back from a messenger has had its Exif rebuilt from nothing. No camera, no timestamp, no coordinates — every question findpic normally answers is answered by tags that are not there.

**They cannot be recovered from the file.** findpic checks rather than assumes: it walks the Exif block and marks every byte reachable from the directory graph, and on a properly stripped file the unreferenced remainder is two bytes of zero padding. The GPS pointer is not present-and-zeroed, it is absent from the directory. There is no slack space to carve.

What survives is the compression, because the compression is the picture. The quantization tables, the restart interval and the segment order are chosen by whoever wrote the file and cannot be removed without encoding it again. So findpic reads those instead, and draws the distinction the reader actually cares about:

```
 FINDINGS
 Originality
  - The file was repackaged, but the picture was not compressed again
    The compression tables belong to the device that took the photo, while the wrapper
    around them — a JFIF header, the tables split across several segments and placed
    after the frame — belongs to a general-purpose library. So the compressed picture
    was copied through untouched and only the container was rebuilt. That is what
    removing metadata looks like from the inside. Your image has lost no quality; it has
    lost its tags.

  - The embedded preview was carried through untouched
    The preview and the picture disagree about how a JPEG should be laid out — the
    preview keeps the device's arrangement, the picture around it has the rewriter's. So
    whatever rebuilt this file copied the preview across as an opaque block without
    looking inside it.
```

Two facts come out of the tables with no signature database at all. Whether they **are** the library's — libjpeg and everything built on it scale the two example tables from Annex K of the JPEG standard, so an exact match names the quality outright, and a near miss is reported as no match rather than as "about 92". And whether the first two rows of the luma table repeat, which no scaling of Annex K can produce, so a table where they do was not written by a library.

Where a timestamp genuinely does survive, findpic says so and hands you the command:

```
  - The capture time survives in the filename: 2023:08:13 14:54:35
    ...that came from the same clock as the deleted tag. This is the one piece of what
    was removed that you can genuinely put back.
    fix: exiftool -AllDates="2023:08:13 14:54:35" -o restored.jpg "IMG_20230813_145435.jpg"
```

`IMG-20230813-WA0002.jpg` gets a date and an explicit "the hour is not known" — the trailing digits are a counter. `IMG_2781.JPG` gets nothing at all, because Apple has never put a date in a filename and a confident guess there would be pure invention.

### Back it up before you need it

Metadata is restorable only if a copy exists. `--backup` writes a sidecar in exiftool's own MIE format:

```bash
findpic photo.jpg --backup                    # writes photo.jpg.mie beside it
findpic stripped.jpg --restore photo.jpg.mie  # writes stripped.restored.jpg
findpic stripped.jpg --restore original.jpg   # any donor that still has its tags
```

Measured on a real iPhone photo: 20 KB of sidecar restores **165 tags of 166**, every value byte-identical, binary MakerNotes included. The one casualty is Apple's `AROT` HDR block, an APP10 segment exiftool can read and cannot write.

Both operations write a new file and neither touches an input. An existing output is refused rather than overwritten — whoever is running this has already lost data once.

> A plain tag dump is **not** a backup. Values are formatted for reading, binary tags are described rather than included, and exiftool reads the result as a list of filenames. findpic's Telegram bot says so on the dump itself and offers the sidecar beside it.

## Three verdicts, deliberately separate

Most tools collapse everything into one score. That destroys the information you actually want, because the axes are independent:

| Axis | Question | Bands |
|---|---|---|
| **Originality** | Has this been edited or re-saved since capture? | `ORIGINAL` · `LIKELY ORIGINAL` · `MODIFIED` · `INCONSISTENT` · `UNKNOWN` |
| **Privacy** | What does it give away about you? | `CLEAN` · `LOW` · `MODERATE` · `HIGH EXPOSURE` |
| **Structure** | Is anything hiding in the file? | `CLEAN` · `MINOR ANOMALIES` · `SUSPICIOUS` · `HIGH RISK` |

A holiday photo straight off your phone is a perfect original *and* a serious privacy leak. A scrubbed meme is a privacy non-event *and* completely unverifiable. One number cannot say that.

**`UNKNOWN` is a real answer.** A file with no metadata gets `UNKNOWN` for originality — never "suspicious". Absence of evidence is reported as absence of evidence.

All three are shown in the CLI, where a banner costs three lines of a tall terminal. The bot shows originality alone and folds privacy into **what this gives away** — on a phone, three coloured labels push the actual content below the fold to summarise information the message is about to show anyway.

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

The samples in this README are built the same way:

```bash
python scripts/make-demo.py demo.jpg
findpic demo.jpg
```

Regenerate them after touching a renderer. Every stale line in this file got there by someone changing a renderer and having no way to notice.

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
  interpret.py     raw measurements  →  sentences (bearing, speed, light, distance)
  jpegprint.py     compression structure  →  which encoder wrote this
  recover.py       capture times that outlived the tags (filenames)
  restore.py       metadata backup and restore (the only code that writes)
  locales/         en.json, uk.json
  models.py        Report, Finding, Verdict — the only thing renderers see
  tables.py        editors, AI generators, filename patterns, encoder digests
  analysis/
    extract.py     raw tags  →  structured fields
    registry.py    the @rule decorator
    rules_*.py     authenticity, privacy, structure, ai, platform, recovery
    verdict.py     the three-axis scoring model
  render/
    terminal.py    the rich report
  bot/
    archive.py     keeping a copy of every picture, on disk
    filenames.py   what a stranger called their file, made safe to be a path
    handlers.py    commands, buttons, media routing
    format.py      the report as a Telegram message
    keyboards.py   reply and inline keyboards, callback factories
    middlewares.py language, usage record, allowlist, rate limit
    service.py     download → analyse → delete
    setup.py       publishes name, descriptions, menu and avatar
    storage.py     language, quota, button handles, who used the bot
scripts/
  bot-stats.py     who used the bot — stdlib only, runs anywhere
  make-demo.py     rebuilds the sample photo in this README
  rotate-token.sh  replaces the bot token everywhere it is stored
```

## Licence

MIT.
