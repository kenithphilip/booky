# Comics and manga (v5.7)

Readers request comics and manga on the portal's **Comics** page. Nothing is read online: each
issue or volume arrives on the reader's own devices, the same way books do.

| Device | How it gets there |
|---|---|
| Kobo (Libra Colour, Clara Colour) | the Kobo link the reader already has (Calibre-Web Kobo sync): a colour, fixed-layout KEPUB made by KCC |
| Kindle (Colorsoft) | Send-to-Kindle mail, like books (auto-send if the reader turned it on): a KCC "Send to Kindle" EPUB, made when it is sent, never stored |
| Phone / iPad | My books → Download: the CBZ (Panels, Chunky, KOReader) or the KEPUB |

**Where comics live.** In the same Calibre-Web library as the books, tagged Comics / Manga /
Manhwa / Manhua, with the series and number set and the reader's owner tag. Shelfmark only
downloads them; it is not a library. So no separate comic server (Komga, Kavita) is needed: the
Kobo sync, Send to Kindle, family sharing, backups and My books already cover them.

## The path of one request (built on Shelfmark, like books)

1. **Metadata.** Western comics: Metron (free account; its API key is sent as a Bearer token),
   ComicVine as a fallback (free key).
   Manga, manhwa, manhua: MangaUpdates (no key). Cached in the portal's database.
2. **Family copy first.** An issue or volume the family already has is given to the reader
   (their owner tag is added), nothing is downloaded.
3. **Search through Shelfmark.** The portal asks Shelfmark (`GET /api/releases`, manual query,
   source Prowlarr, category Books 7000, which includes Comics 7030) with a few spellings of the
   series and number, and scores what comes back (below).
4. **Queue through Shelfmark, as the reader.** `POST /api/releases/download` with
   `on_behalf_of_user_id` = the reader's Shelfmark account. From here it is Shelfmark's normal
   download: the seedbox's SABnzbd or rTorrent with Shelfmark's ebook category/label, the
   .torrent fetched through the seedbox login (IP-locked trackers), **torrents kept seeding on
   the seedbox** (`PROWLARR_TORRENT_ACTION=keep`), Usenet copied, the file brought home by
   Syncthing and handed to Shelfmark by the path mappings, which delivers it into the reader's
   dropbox. The portal never starts, stops, removes or relabels anything on the seedbox.
5. **Import.** The dropbox watcher sees a CBZ/CBR/CB7: CBR and CB7 are repacked as CBZ
   (`unar`, which also opens solid RAR 3/4 archives that libarchive cannot), the file is matched to the reader's open comic request, ComicInfo.xml and a
   ComicBookInfo block (series, number, credits, publisher, year, tags Comics/Manga and
   owner:<reader>) are written into it, and it is imported into Calibre-Web like any book.
6. **Kobo copy.** Only when an owner of the comic reads on a Kobo (Calibre-Web has synced books
   to it), or a reader presses **Make Kobo copy**: a reader who only uses an iPad never costs a
   conversion. The host job `scripts/comic-convert.sh` (cron, one at a time, memory-capped)
   runs KCC on the CBZ and adds the result to the SAME Calibre book as its `KEPUB` format.
   Calibre-Web's Kobo sync prefers a stored KEPUB and sends a pre-paginated one as `EPUB3FL`
   (fixed layout), so it reaches the Kobo through the existing link. Calibre-Web's cover and
   metadata enforcer only rewrites .epub/.azw3, so the KEPUB is never rewritten.

## Classification (where comic automation usually goes wrong)

**The series** (from the metadata provider, stored on the request, overridable by the reader):

| Kind | Reading | KCC |
|---|---|---|
| Western comic | left to right | normal |
| Manga | right to left | `-m` |
| Manhwa / manhua / webtoon | left to right, long strip | `-w` |
| Light novel / novel | text | not a comic: offered as a book request instead |

**A release** (parsed from its title): series, volume, issue or chapter range, year, edition
(omnibus, 3-in-1, deluxe, annual), digital or scan, group, language markers, pack range
(`v01-v10`), format.

**Scoring** (a release must pass every hard rule, then the best score wins):
- hard: the series matches; the number is the one requested (or a pack that contains it); a
  chapter release never fills a volume request; the language is the request's (explicit other
  languages and raws are dropped); the format is CBZ, CBR, CB7, PDF or EPUB; the size is sane.
- preferred: a single issue/volume over a pack; digital over scan; official publisher names over
  fan groups; Usenet over torrent at equal quality (no seeding needed); more seeders.
- nothing passes: the request waits and is searched again (1 h, 6 h, then daily, for 30 days).

From a pack, only the requested volume is imported; the rest of the pack stays on the seedbox
(seeding) and is not imported for anyone.

7. **Kindle copy.** Made when a comic is sent (button or auto-send), never stored: KCC's
   Send-to-Kindle EPUB for the Colorsoft, at lower image quality if it is over the mail limit,
   split into parts if it still is. The portal mails the parts and deletes them.

Reading progress comes back from the Kobo (Calibre-Web records it); a Kindle or an iPad never
reports it. v5.8: **Follow** on a series page puts new issues or volumes (once released) on the
reader's New for you list with one-tap Request; AniList (Devices) gets the reader's finished
manga volumes. Following chapters and swapping them for the volume come in v5.9.

## What the owner sets up

- Shelfmark → Settings → Formats: tick **CBR** (EPUB, PDF, CBZ are already on).
- A free Metron account's API key (metron.cloud; its user name and password work too), optionally
  a ComicVine API key: TUI Library → Comics.
- Nothing on the seedbox: comics use Shelfmark's existing ebook category and label.


## v5.9: chapters, safeguards, reading status, Metron

**Chapters, then the volume.** On a manga series (MangaUpdates): *Follow chapters, then volumes*.
- New chapters: MangaUpdates' `latest_chapter`; the first check only records what is out.
- A chapter request is judged like a volume: the series, the chapter number (a chapter pack, a
  volume or another chapter is refused), the language; searched as "<series> chapter <n>", then
  the series name. Measured on the owner's indexers (2026-09-29): Chainsaw Man 9 chapter
  releases of 42, One Piece 14 of 316, Kagurabachi none. Not found in 7 days: it says so.
- Chapters are imported into their own Calibre series, `<series> (chapters)`, tagged `Chapter`.
- The volume: its New for you item says which chapters it holds (MangaDex `GET /manga/{id}/
  aggregate`, only for the MangaDex entry whose `links.mu` is this series' MangaUpdates id in base
  36). Once the volume is in the reader's library (downloaded or shared), Comics offers to remove
  their chapters of it, pre-ticked; MangaDex's lists can be untidy, so the reader decides.

**Safeguards** (the same as for books): the reader confirms each copy (Yes to all per series;
`COMIC_CONFIRM=sure` skips it for the exact digital issue/volume in their language); each arrival
is checked (a ComicInfo.xml it carries: Series, Number/Volume, LanguageISO; its name's language
markers; the release name without a number; a sensible page count) and held for the reader when
it does not match; **Wrong comic** takes a delivered comic out of their library, blocks that
release and that Calibre book, tells the admin and looks again.

**Reading status by hand** for a Kindle or an iPad (which report nothing): Read / Reading /
Unread on each book, *read up to here* on a series; written to Calibre-Web's `book_read_link`.

**Metron** for Western comics: Devices → Connect Metron; finished issues found through Metron are
scrobbled (`POST /api/collection/scrobble/`) to the reader's own collection, once each.
