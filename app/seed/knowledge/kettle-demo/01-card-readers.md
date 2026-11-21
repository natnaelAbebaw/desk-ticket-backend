---
title: Card readers
author: owner
---

# Card readers

Kettle supports the K2 countertop reader and the K2 Go handheld reader. Both connect to
the Kettle POS app over Bluetooth and take chip, tap, and phone payments.

## Pairing a reader

Turn the reader on and hold the power button for 5 seconds until the light blinks blue.
In the POS app open Settings, then Hardware, then Add reader, and pick the reader whose
serial number matches the sticker on the back. A reader can be paired with one tablet at
a time; pairing it with a second tablet unpairs it from the first.

## Status lights

- Solid green: ready to take payments.
- Blinking blue: waiting to pair.
- Blinking amber: updating firmware. Do not turn it off.
- Solid red: battery below 5 percent. Plug it in before the next payment.

## Error codes

| Code | Meaning | What to do |
|------|---------|------------|
| K-101 | Reader not found | Turn Bluetooth off and on, then pair again |
| K-207 | Declined by the card issuer | Ask for another card; Kettle cannot override it |
| K-310 | Firmware update failed | Keep the reader charging and retry from Settings, Hardware |
| K-415 | Reader tampered | Stop using it and contact support for a replacement |

A K-415 reader locks itself for security. It cannot be unlocked remotely and must be
replaced, free of charge under warranty.

## Offline mode

If the internet drops, the reader keeps taking chip payments for up to 24 hours and
stores them encrypted. Offline payments are capped at 100 USD each and 2,000 USD in
total. They upload as soon as the connection is back. A card that turns out to be
declined when it uploads is the merchant's loss, so offline mode is off by default and
must be switched on per location in Settings, Payments.
