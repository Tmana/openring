# OpenRing — Hardware

Reference build for a v0.1 doorbell. Substitute freely; OpenRing only
requires a Linux host with a camera that speaks RTSP and a GPIO button.

## Bill of materials

| # | Part | Notes | ~USD |
|---|---|---|---|
| 1 | Raspberry Pi Zero 2 W | Get the W — Wi-Fi is mandatory | 15 |
| 2 | Pi Camera Module 3 (wide, NoIR) | The wide variant fits a porch view; NoIR is essential because no IR-cut filter means it works at night with cheap 850 nm IR LEDs | 35 |
| 3 | 32 GB SanDisk Industrial microSD | Industrial-grade SDs survive the write churn of journald + MediaMTX's small caches; consumer SDs die in months | 12 |
| 4 | 12 mm momentary stainless panel-mount button | NO contacts, IP65, screw-terminal — the actual doorbell | 5 |
| 5 | Mean Well IRM-15-5 (or equivalent 5 V/3 A buck) | Steps existing chime transformer's 16-24 V AC down to 5 V DC | 12 |
| 6 | 3D-printed enclosure | STLs in `hardware/` (TBD); PETG holds up to porch sun, PLA does not | filament |
| 7 | M2.5 hardware kit, JST connectors, 22 AWG hookup wire | grab bag | 5 |
| | **Subtotal v0.1** | | **~$84** |

### Add-ons (v0.3 — two-way audio)

| # | Part | Notes | ~USD |
|---|---|---|---|
| 8 | UGREEN USB-C audio adapter | Pi Zero 2 W has no analog audio out; this is the smallest USB DAC that works with `aplay` out of the box | 10 |
| 9 | INMP441 I2S mic OR USB lavalier mic | Either works; USB is plug-and-play, I2S is one fewer USB port to find | 8 |
| 10 | 1 W 8 Ω speaker | Anything tiny; voice-only audio is forgiving | 5 |
| | **+v0.3 subtotal** | | **+~$23** |

## Power options, ranked

1. **Existing wired doorbell transformer (recommended).** Most North
   American houses have a 16 V or 24 V AC transformer in the basement
   or attic feeding the chime. Swap the chime for a buck converter and
   you're done — the doorbell still rings (we'll wire a small relay on
   the GPIO output if you want the analog chime to keep working in
   parallel; that's a v0.2 wiring guide).
2. **5 V USB-C wall wart run through the wall.** Easiest if you don't
   already have doorbell wiring.
3. **PoE.** Uses a $15 PoE splitter; nice if you've already run Cat6 to
   the porch for an IP camera. Adds bulk to the enclosure.

## Why not battery?

Real-time H.264 + Wi-Fi on a Pi Zero 2 W draws ~1.5-2 W steady. A
modest 3000 mAh USB power bank gives you ~6 hours. The "1-year battery
life" of commercial doorbells comes from a totally different
architecture: deep-sleep with a PIR-driven wake, low-resolution wake
shots only. To match that you'd be writing custom firmware on an
ESP32-S3 with a separate PIR sensor and accepting that the live-view
feature ships a 5-second delay. Not the v0.1 product.

If demand is loud enough we'll fork an `openring-mini` track for an
ESP32-S3 build. Until then: get power to the porch.

## Why not a cellular fallback?

Same reason commercial-doorbell vendors don't ship one in the base
product: it's a recurring SIM cost and it makes the device dependent
on a third party (the carrier and our cell-modem firmware). OpenRing
declines that bargain. If your home internet is unreliable, the
correct fix is your home internet.

## Camera lens choices

| Lens | Field of view | Use case |
|---|---|---|
| Pi Camera Module 3 standard | 75° | Hallway / interior side door |
| Pi Camera Module 3 **wide** | 102° | Porch front door — recommended |
| Camera Module 3 with telephoto add-on | narrow | Long driveway gate |
| Arducam IMX477 + M12 lens | whatever | If you want pro glass and don't mind tinkering |

## Network requirements

- 2.4 GHz Wi-Fi (the Pi Zero 2 W doesn't speak 5 GHz). 802.11n is
  sufficient — H.264 720p15 fits comfortably under 4 Mbit/s.
- Static DHCP reservation for the doorbell, *or* mDNS (Avahi). We
  document both in v0.1.
- Port 8554 (RTSP) reachable from the host. Stays on the LAN — we
  don't expose the doorbell port externally.

## Tested-on (please add a row when you build one!)

| OpenRing version | Pi model | Camera | Power source | Notes |
|---|---|---|---|---|
| v0.1.0-dev | Pi Zero 2 W | Camera Module 3 Wide NoIR | 24 VAC chime → IRM-15-5 | reference build |
