# QIC standards library

This page lists every QIC development standard in `docs/qic-standards/`,
with its revision, date and a one-line summary. The standards were published
by Quarter-Inch Cartridge Drive Standards, Inc. The first table holds the
ones Tapewyrm implements or cites; the rest are kept for reference.

Titles and dates are from each document's cover page. Where a cover is a
scanned image, it was read from the image.

## Standards Tapewyrm uses

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-40-MC | M | 02 Sep 92 | Floppy-controller-compatible recording format: 20 tracks, 10,000 bpi MFM, 40 MB on a DC2000. | [qic40m.pdf](../qic-standards/qic40m.pdf) |
| QIC-80-MC | N | 20 Mar 96 | Floppy-controller-compatible recording format: 28 or 36 tracks, 14,700 bpi MFM, segments, ECC, header and volume table. | [qic80n.pdf](../qic-standards/qic80n.pdf) |
| QIC-3010-MC | H | 27 Aug 97 | Floppy-tape recording format on 900 Oe tape: 40 or 50 tracks, 22,125 bpi MFM. | [qic3010h.pdf](../qic-standards/qic3010h.pdf) |
| QIC-3020-MC | H | 20 Mar 96 | Floppy-tape recording format on 900 Oe tape: 40 or 50 tracks, 44,250 bpi MFM; format code 6. | [qic3020h.pdf](../qic-standards/qic3020h.pdf) |
| QIC-117 | J | 28 Aug 96 | Command set interface for floppy-controller-based drives: pulse-count commands, reports, error codes, vendor makes. | [qic117j.pdf](../qic-standards/qic117j.pdf) |
| QIC-113 | G | 15 Jun 95 | Host interchange format: volume table extensions, Basic DOS and extended file sets, volume compression. | [qic113g.pdf](../qic-standards/qic113g.pdf) |
| QIC-122 | B | 06 Feb 91 | Data compression format (Stac LZS) with a 2,048-byte history. | [qic122b.pdf](../qic-standards/qic122b.pdf) |
| QIC-123 | D | 01 Sep 94 | Registry of data algorithm identifiers: the compression codes in a volume table entry. | [qic123d.pdf](../qic-standards/qic123d.pdf) |
| QIC-CRF3 | C | 21 Mar 96 | Common recording format for the floppy-controller formats (QIC-40, QIC-80, QIC-3010, QIC-3020). | [crf3c.pdf](../qic-standards/crf3c.pdf) |
| QIC-100-MC | F | 23 May 89 | GCR minicartridge format for floppy-interface drives: 24 tracks, 12,500 ftpi, 86 MB on a DC2080. Not QIC-80; only cinnabar's synthetic decoder uses it. | [qic100mc.pdf](../qic-standards/qic100mc.pdf) |

## Drive interfaces and command sets

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-107 | A | 05 Jun 86 | Basic drive interface for floppy-controller-compatible minicartridge drives. | [qic107a.pdf](../qic-standards/qic107a.pdf) |
| QIC-115 | A | 03 Feb 88 | Basic drive interface for PS/2 floppy-controller-compatible minicartridge drives. | [qic115a.pdf](../qic-standards/qic115a.pdf) |
| QIC-146 | A | 02 Dec 92 | Autoloader SCSI gateway protocol, serial bus implementation. | [qic146a.pdf](../qic-standards/qic146a.pdf) |
| QIC-157 | D | 13 Dec 95 | Common SCSI/ATAPI command set for streaming tape. | [qic157d.pdf](../qic-standards/qic157d.pdf) |
| QIC-172 | A | 30 Aug 95 | Command descriptor block (CDB) interface for floppy tape. | [qic172a.pdf](../qic-standards/qic172a.pdf) |

## Logical formats, ECC and compression

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-110 | A | 19 Apr 88 | Logical information interchange on a 26-track, 20,000 ftpi data cartridge. | [qic110a.pdf](../qic-standards/qic110a.pdf) |
| QIC-112 | A | 27 Oct 87 | ECC format. | [qic112a.pdf](../qic-standards/qic112a.pdf) |
| QIC-130 | C | 03 Sep 92 | DCLZ data compression format. | [qic130c.pdf](../qic-standards/qic130c.pdf) |
| QIC-154 | A | 10 Mar 94 | Adaptive lossless data compression (ALDC). | [qic154a.pdf](../qic-standards/qic154a.pdf) |
| QIC-174 | A | 21 Mar 96 | CD-ROM compatible tape format for an installable file system. | [qic174a.pdf](../qic-standards/qic174a.pdf) |
| QIC-CRF1 | J | 15 Jun 95 | Common recording format with multichannel capability for RLL 1,7 formats (QIC-3230, QIC-5010, QIC-5210). | [crf1j.pdf](../qic-standards/crf1j.pdf) |

## Minicartridge recording formats

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-3040-MC | E | 20 Jun 96 | Read-while-write minicartridge format: 42 or 52 tracks, 50,800 ftpi. | [qic3040e.pdf](../qic-standards/qic3040e.pdf) |
| QIC-3080-MC | C | 15 Dec 94 | Minicartridge format on preformatted 900 Oe tape: RLL 1,7, 60,000 bpi. | [qic3080c.pdf](../qic-standards/qic3080c.pdf) |
| QIC-3210-MC | B | 20 Jun 96 | Read-while-write minicartridge format: 56 or 72 tracks, 76,200 ftpi. | [qic3210b.pdf](../qic-standards/qic3210b.pdf) |
| QIC-3220-MC | A | 27 Aug 97 | Minicartridge format on 0.315 in preformatted 1,650 Oe tape: 108 tracks, RLL 1,7. | [qic3220a.pdf](../qic-standards/qic3220a.pdf) |

## Data cartridge recording formats

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-120-DC | F | 20 Jun 91 | 15 tracks, 10,000 bpi GCR, 125 MB. | [qic120dc.pdf](../qic-standards/qic120dc.pdf) |
| QIC-525-DC | H | 10 Mar 94 | 26 tracks, 20,000 ftpi GCR, 525 MB on a DC6525. | [qic525h.pdf](../qic-standards/qic525h.pdf) |
| QIC-1000-DC | E | 10 Mar 94 | 30 tracks, 45,000 ftpi GCR, 1.01 GB on a DC9100. | [qic1000e.pdf](../qic-standards/qic1000e.pdf) |
| QIC-2GB-DC | B | 10 Mar 94 | 42 tracks, 50,800 ftpi GCR. | [qic2gbb.pdf](../qic-standards/qic2gbb.pdf) |
| QIC-4GB-DC | A | 24 Apr 97 | 46 tracks, 62,000 ftpi GCR. | [qic4gba.pdf](../qic-standards/qic4gba.pdf) |
| QIC-5010-DC | E | 15 Dec 94 | 144 data tracks, 50,800 ftpi RLL 1,7. | [qic5010e.pdf](../qic-standards/qic5010e.pdf) |
| QIC-5210-DC | A | 31 Aug 95 | 144 data tracks, 76,200 ftpi RLL 1,7. | [qic5210a.pdf](../qic-standards/qic5210a.pdf) |

## Recording heads

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-101 | B | 10 Dec 85 | Head for QIC-150-DC (superseded by QIC-105). | [qic101b.pdf](../qic-standards/qic101b.pdf) |
| QIC-102 | A | 10 Dec 85 | Head for QIC-100-MC. | [qic102a.pdf](../qic-standards/qic102a.pdf) |
| QIC-105 | A | 08 Apr 86 | Head for QIC-120-DC. | [qic105a.pdf](../qic-standards/qic105a.pdf) |
| QIC-106 | C | 11 Feb 87 | Head for the QIC-40 format (the cover says "QIC-40-MB"). | [qic106c.pdf](../qic-standards/qic106c.pdf) |
| QIC-114 | B | 18 Jan 89 | Head for QIC-525-DC. | [qic114b.pdf](../qic-standards/qic114b.pdf) |
| QIC-118 | B | 14 Jun 95 | Head for QIC-80-MC. | [qic118b.pdf](../qic-standards/qic118b.pdf) |
| QIC-119 | E | 13 Jun 90 | Head for QIC-1350-DC. | [qic119e.pdf](../qic-standards/qic119e.pdf) |
| QIC-126 | D | 15 Jun 94 | Head for QIC-1000-DC, 2 GB data cartridge and QIC-3040-MC drives. | [qic126d.pdf](../qic-standards/qic126d.pdf) |
| QIC-131 | B | 02 Dec 92 | Head for QIC-3030-MC. | [qic131b.pdf](../qic-standards/qic131b.pdf) |
| QIC-133 | G | 27 Aug 97 | Head for QIC-3010-MC and QIC-3020-MC. | [qic133g.pdf](../qic-standards/qic133g.pdf) |
| QIC-134 | F | 27 Aug 97 | Head for QIC-5010-DC. | [qic134f.pdf](../qic-standards/qic134f.pdf) |
| QIC-151 | B | 15 Jun 94 | Head for QIC-3050-MC. | [qic151b.pdf](../qic-standards/qic151b.pdf) |
| QIC-158 | B | 20 Jun 96 | Head for QIC-3080-MC. | [qic158b.pdf](../qic-standards/qic158b.pdf) |
| QIC-171 | C | 11 Dec 96 | Head for QIC-3095-MC. | [qic171c.pdf](../qic-standards/qic171c.pdf) |
| QIC-177 | A | 27 Aug 97 | Head for QIC-3220-MC. | [qic177a.pdf](../qic-standards/qic177a.pdf) |
| QIC-178 | A | 27 Aug 97 | Head for QIC-3210-MC. | [qic178a.pdf](../qic-standards/qic178a.pdf) |
| QIC-179 | A | 27 Aug 97 | Head for QIC-4GB-DC. | [qic179a.pdf](../qic-standards/qic179a.pdf) |
| QIC-181 | A | 27 Aug 97 | Head for QIC-5210-DC. | [qic181a.pdf](../qic-standards/qic181a.pdf) |

## Cartridges and media

These define the physical cartridges. The ones for QIC-80 and QIC-3010/3020
tapes are marked in bold.

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-136 | H | 23 Apr 97 | Unrecorded 5.25 in data cartridge, 0.25 in, 900 Oe (QIC-1000-DC, QIC-2GB-DC). | [qic136h.pdf](../qic-standards/qic136h.pdf) |
| QIC-137 | H | 20 Mar 96 | Unrecorded 5.25 in data cartridge, 0.25 in, 900 Oe (QIC-1350-DC, QIC-2100-DC). | [qic137h.pdf](../qic-standards/qic137h.pdf) |
| QIC-138 | D | 20 Mar 96 | Unformatted servo-written 3.5 in minicartridge, 0.25 in, 50,800 ftpi, 900 Oe (DC 3GB). | [qic138d.pdf](../qic-standards/qic138d.pdf) |
| QIC-139 | L | 23 Apr 97 | Unformatted servo-written 5.25 in cartridge, 0.25 in, 900 Oe (QIC-5010-DC). | [qic139l.pdf](../qic-standards/qic139l.pdf) |
| QIC-140 | A | 26 Feb 92 | Read-only unrecorded 3.5 in minicartridge, 0.25 in, 550 Oe. | [qic140a.pdf](../qic-standards/qic140a.pdf) |
| QIC-141 | A | 26 Feb 92 | Read-only unrecorded 5.25 in cartridge, 0.25 in, 550 Oe. | [qic141a.pdf](../qic-standards/qic141a.pdf) |
| **QIC-143** | G | 20 Mar 96 | Unrecorded 3.5 in minicartridge, 0.25 in, 900 Oe, 300 and 400 ft (QIC-3010 to QIC-3050). | [qic143g.pdf](../qic-standards/qic143g.pdf) |
| **QIC-148** | D | 20 Jun 96 | Unrecorded 3.5 in minicartridge, 0.315 in, 900 Oe (QIC-3010, 3020, 3040, 3080, 3095 wide). | [qic148d.pdf](../qic-standards/qic148d.pdf) |
| QIC-153 | D | 20 Mar 96 | Unrecorded minicartridge, 0.25 in, 1,400 Oe barium ferrite. | [qic153d.pdf](../qic-standards/qic153d.pdf) |
| QIC-156 | D | 20 Mar 96 | Unrecorded minicartridge, 0.25 in, 900 Oe, 400 ft (QIC-3080). | [qic156d.pdf](../qic-standards/qic156d.pdf) |
| **QIC-159** | C | 20 Jun 96 | Unrecorded minicartridge, 0.315 in, 550 Oe, 400 ft (QIC-80 wide, 208 MB). | [qic159c.pdf](../qic-standards/qic159c.pdf) |
| **QIC-160** | A | 14 Dec 94 | Unrecorded minicartridge, 0.25 in, 550 Oe, 425 ft (QIC-80, 170 MB). | [qic160a.pdf](../qic-standards/qic160a.pdf) |
| **QIC-161** | D | 13 Dec 95 | Unrecorded minicartridge, 0.315 in, 550 Oe, 750 ft (QIC-80 wide, 400 MB). | [qic161d.pdf](../qic-standards/qic161d.pdf) |
| **QIC-162** | E | 20 Mar 96 | Unrecorded minicartridge, 0.315 in, 900 Oe, 750 ft (QIC-3010 and QIC-3020 wide). | [qic162e.pdf](../qic-standards/qic162e.pdf) |
| QIC-163 | C | 20 Jun 96 | Unrecorded minicartridge, 0.315 in, 1,800 Oe, 400 ft (QIC-3210). | [qic163c.pdf](../qic-standards/qic163c.pdf) |
| QIC-164 | D | 20 Mar 96 | Servo-burst, track-ID minicartridge, 0.315 in, 900 Oe, 740 ft (QIC-3095). | [qic164d.pdf](../qic-standards/qic164d.pdf) |
| **QIC-165** | C | 28 Aug 96 | Unrecorded cartridge, 0.25 in, 900 Oe, 1,000 ft (QIC-3010 to QIC-3050). Scanned cover. | [qic165c.pdf](../qic-standards/qic165c.pdf) |
| **QIC-166** | C | 28 Aug 96 | Unrecorded cartridge, 0.315 in, 900 Oe, 1,000 ft (QIC-3010 to QIC-3050). Scanned cover. | [qic166c.pdf](../qic-standards/qic166c.pdf) |
| **QIC-167** | B | 28 Aug 96 | Unrecorded cartridge, 0.25 in, 550 Oe, 1,000 ft (QIC-80). Scanned cover. | [qic167b.pdf](../qic-standards/qic167b.pdf) |
| **QIC-168** | B | 28 Aug 96 | Unrecorded minicartridge, 0.315 in, 550 Oe, 1,000 ft (QIC-80 wide, 500 MB). | [qic168b.pdf](../qic-standards/qic168b.pdf) |
| QIC-169 | D | 23 Apr 97 | Unformatted servo-written cartridge, 0.25 in, 1,650 Oe, 1,500 ft. | [qic169d.pdf](../qic-standards/qic169d.pdf) |
| QIC-170 | D | 13 May 96 | Preformatted minicartridge, 0.315 in, 900 Oe: the preformatting used by QIC-164 and QIC-173. | [qic170d.pdf](../qic-standards/qic170d.pdf) |
| QIC-173 | C | 28 Aug 96 | Servo-burst, track-ID cartridge, 0.315 in, 900 Oe, 990 ft (QIC-3095). | [qic173c.pdf](../qic-standards/qic173c.pdf) |
| QIC-175 | B | 28 Aug 96 | Unrecorded cartridge, 0.315 in, 1,850 Oe, 1,000 ft (QIC-3210). | [qic175b.pdf](../qic-standards/qic175b.pdf) |
| QIC-180 | A | 27 Aug 97 | Servo-burst, track-ID minicartridge, 0.315 in, 1,650 Oe, 740 ft (QIC-3220). | [qic180a.pdf](../qic-standards/qic180a.pdf) |

## Cleaning and identification

| Standard | Revision | Date | What it covers | PDF |
|---|---|---|---|---|
| QIC-147 | C | 13 Dec 95 | Cleaning cartridge recognition. Scanned; no text layer. | [qic147c.pdf](../qic-standards/qic147c.pdf) |
| QIC-176 | A | 28 Aug 96 | Cleaning cartridge recognition with a light prism. | [qic176a.pdf](../qic-standards/qic176a.pdf) |
| QIC 90-14 | S | 24 Apr 97 | 5.25 in data cartridge identification table (QIC-121 table 9-20A). | [qic90-14s.pdf](../qic-standards/qic90-14s.pdf) |
| QIC 91-16 | S | 24 Apr 97 | 3.5 in minicartridge identification table (QIC-121 table 9-20A). | [qic91-16s.pdf](../qic-standards/qic91-16s.pdf) |
| QIC 95-101 | E | 25 Sep 97 | Medium type and density codes (a Hewlett-Packard proposal). | [qic95-101e.pdf](../qic-standards/qic95-101e.pdf) |

## See also

- [Formats](index.md)
- [Cartridges](../hardware/cartridges.md)
