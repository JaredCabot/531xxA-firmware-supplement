# 531xxA Firmware and Service Notes

Reverse-engineering notes and service tools for the Agilent / HP 53131A, 53132A
and 53181A universal counters. The centerpiece is a Firmware and Service
Supplement (PDF) that documents the counter firmware: EPROM image construction,
the non-volatile calibration and configuration store, the Channel 3 prescaler
option, the hidden service and diagnostic menus, and the undocumented commands,
including the embedded pForth monitor.

This is an independent project. It is not a product of Agilent Technologies,
Hewlett-Packard or Keysight, and carries no endorsement.

## What is here

- `531xxA_Firmware_Supplement.pdf` - the full reference document. Read this for
  the detail; the tools below are companions to it.
- `531xx_eeprom_tool.py` - offline editor for a saved dump of the counter's NVRAM
  (the AT28C64B EEPROM at CPU address $400000, U14 on the 53181A; read it with a
  programmer). Reads and decodes the store, changes the Channel 3 option, and can
  factory-blank the image to reset a forgotten calibration security code. This is
  a small configuration/calibration chip, separate from the program firmware below.
- `531xx_gpib_tool.py` - live service tool over GPIB. Identifies the counter,
  backs up and restores calibration data, sets the Channel 3 option, manages the
  calibration security code and sends raw SCPI.
- Program firmware image archives for the three counters (`*.zip`) - the U8-U11
  program EPROMs (four 128 KB devices per instrument), provided for reference, for
  disassembly, and for reburning EPROMs. These hold the instrument's firmware and
  are a different thing from the U14 NVRAM the EEPROM tool edits; the EEPROM tool
  does not operate on them.

## Requirements

- Python 3.6 or newer.
- The EEPROM tool uses only the Python standard library. No packages needed.
- The GPIB tool needs PyVISA and a VISA backend:

  ```
  pip install pyvisa
  ```

  You then need one backend. Any of these works:
  - A vendor VISA runtime (Keysight IO Libraries, NI-VISA, or similar), or
  - the pure-Python backend, which needs no vendor runtime:

    ```
    pip install pyvisa-py pyusb pyserial
    ```

  If you drive GPIB through a Prologix GPIB-USB adapter instead of a VISA
  interface, install `pyserial`:

  ```
  pip install pyserial
  ```

## EEPROM tool

Works on an 8192-byte binary dump of the counter's parallel EEPROM (the
AT28C64B-class device at CPU address $400000). Read the chip with any programmer
to make the dump, edit it with this tool, then write it back. Nothing here talks
to the instrument; it only edits dump files, so it is safe to explore. Always
keep the original dump.

Run it, optionally passing a dump file to load on start:

```
python 531xx_eeprom_tool.py
python 531xx_eeprom_tool.py mydump.bin
```

It presents a menu:

```
  1) Load dump file
  2) Show info / decode
  3) Backup (timestamped copy)
  4) Restore from backup
  5) Change Channel 3 option
  6) Reset calibration password (help + factory-blank)
  7) Save dump
  8) Quit
```

Typical use: load a dump (1), decode it (2), make a backup (3), change the
option or blank the image (5 or 6), then save (7) and program the result back to
the chip.

## GPIB tool

Talks to a live counter over GPIB. Run it on the machine the GPIB interface is
attached to:

```
python 531xx_gpib_tool.py
```

It scans the bus, identifies the counter and presents a menu:

```
  1) Rescan / choose instrument
  2) Show info (IDN, OPT, Channel 3, cal status)
  3) Back up calibration data (:CAL:DATA?)
  4) Restore calibration data (:CAL:DATA)
  5) Change Channel 3 option (experimental)
  6) Calibration security code
  7) Send raw SCPI
  8) Read error queue (:SYST:ERR?)
  9) Quit
```

Useful options:

- `--visa` forces the PyVISA transport.
- `--prologix --port COM4` uses a Prologix GPIB-USB adapter on the given serial
  port (needs `pyserial`).
- `--selftest` runs against a built-in mock instrument, with no hardware, so you
  can try the menus safely.

Before changing anything, use option 3 to save the calibration block to a file.
Option 4 restores it.

## Safety

The tools can change stored calibration and configuration. The manual's
procedures, and options 5 and 6 in each tool, can erase calibration and require
the instrument to be recalibrated against traceable standards. Back up first
(the EEPROM dump, and the `:CAL:DATA?` block over GPIB), and read the cautions in
the supplement before writing to an instrument.
