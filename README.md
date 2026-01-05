# Nelko P21 label printer script and acapture
This is a wireshark capture of the bluetooth traffic of a Nelko P21 label printer and a resulting simple python script, that makes it possible to print labels without the offical app.

## Script usage

The printer works over a Bluetooth classic connection using the serial protocol (sometimes called SPP or RFCOMM). To establish a connection to the printer power it on and pair it using any Bluetooth connection tool. Then create an RFCOMM connection, either by using your tool of choice or by using bluez' `rfcomm` cli tool:

```bash
$ rfcomm connect /dev/rfcomm0 XX:XX:XX:XX:XX:XX
```

Make sure to replace the `XX:XX...` part with the Bluetooth MAC of your printer. When the connection was successful, you can start the script. I recommend creating a virtual environment and installing the requirements via `pip`. The script will print a help screen on start.

## The captured traffic and the printers protocol

It contains a connection and a print of the default template on a 14x40mm label. The entire communication of the printer runs via SPP/RFCOMM aka a serial connection over Bluetooth. The printer also has an internal NFC reader to identify the the label rolls put inside. It automatically determines the format of the labels this way. It also seems to be a type of soft DRM, where the app complains, if you use third-party label rolls.

The printer itself uses some proprietary commands like the following. Every command must be followed by a CRLF as is every response. 
- `BATTERY?`  
  Responds with: `BATTERY ` followed by two bytes. The first byte is most likely the charge level in percent.
- `CONFIG?` 
  Responds with: `CONFIG ` followed by something like `00cb0000030402040201`. 
  The first byte may indicate some protocol type, in this case TSPL2 and the second to the DPI resolution of 203 (CB).
  The next three bytes `00 00 03` corresponds to the first firmware version in the app (0.3.0).
  The three bytes after that `04 02 04` corresponds to the second firmware version in the app (4.2.4).
  Then comes one byte containing the timeout setting: `00` to `03` for never, 15 min, 30 min, 60 min.
  The last byte is the status of the beep setting.
- `BEEP` followed by a space and 0x00 or 0x01. 
- `[ESC]!o`  
  According to the TSPL2 documentation this cancels the pause status of the printer. The command is sent repeatedly from the app to the printer and the printer answers with a short status.
- `[ESC]!?`
  Seems to return the ready status for the printer.

The sent printing commands correspond to parts of TSPL2:

```plaintext
SIZE 14.0 mm,40.0 mm
GAP 5.0 mm,0 mm
DIRECTION 0,0
DENSITY 15
CLS
BITMAP 0,0,12,284,1,?????AT???GuC??
... [truncated]
```

It only supports a subset of TSPL2 commands like:

- SIZE: Sets the size of the labels.
- GAP: Sets the gap between the labels.
- DIRECTION: Controls the print direction. In case of the P21 it doesn't seem to change anything.
- DENSITY: Controls the print density/darkness of the print.
- CLS: Clears the print canvas.
- BITMAP: Prints an image and takes the parameters Xpos, YPos, height in bytes, width in dots.
- SELFTEST: This triggers the test print, the printer generates when hitting the power button once.
- PRINT x: Prints x copies of the label
- BAR: prints only a completely black label
- BARCODE: might do something, but doesn't correspond to the TSPL2 syntax. I saw it print a slightly messy black bar. I skipped all other barcode commands, after checking if QRCODE works. It doesn't.
- INITIALPRINTER: Triggers a factory reset.

The image format is 96x284 pixels in 1 bit color depth as raw data. Every bit is a pixel there are no checksums or error correction data.

The printer also exposes a serial USB connection to the PC but only returns `ERROR0` on any command.

Internally it uses a JieLi AC6951C (or similiar) bluetooth chip (see https://github.com/kagaimiq/jielie/pull/6).

Nelkos app also uses JieLis ota update feature. It checks for updates at this url: http://app.nelko.net/api/firmware/verify with a POST request:

```json
{"hardwareName":"0.0.3","dev":"P21","firmwareName":"4.2.4"}
```

There seems to be no way to get the URL for the current firmware. The app is very chatty and even sends the entire device metadata to the server. And seemingly via plain HTTP.
